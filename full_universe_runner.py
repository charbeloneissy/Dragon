import asyncio
import json
import random
import time
from decimal import Decimal

import websockets

import web_runner
from src.dragon.control import analysis_allowed, trading_allowed

WS_BACKOFF_MIN = 1.0
WS_BACKOFF_MAX = 60.0
WS_STALE_SECONDS = 45.0
# Keep combined-stream URLs comfortably below proxy/server URI limits.
WS_SHARD_SIZE = 100
WS_PING_INTERVAL = 20.0
WS_PING_TIMEOUT = 20.0
REST_FALLBACK_SECONDS = 3.0


def _ws_state(worker_id, **values):
    with web_runner.LOCK:
        for key, value in values.items():
            web_runner.STATE[key] = value
        web_runner.STATE.setdefault("ws_reconnects", 0)
        web_runner.STATE.setdefault("ws_disconnects", 0)
        web_runner.STATE.setdefault("ws_last_disconnect", None)
        web_runner.STATE.setdefault("ws_next_retry_at", None)
        web_runner.STATE.setdefault("ws_shard_status", {})
        status = dict(web_runner.STATE["ws_shard_status"])
        status[str(worker_id)] = values.get("status", status.get(str(worker_id), "unknown"))
        web_runner.STATE["ws_shard_status"] = status


async def _feed_worker(cfg, symbols, queue, worker_id):
    streams = [f"{s.lower()}@depth{cfg.depth_levels}@100ms" for s in symbols]
    if not streams:
        return
    base = cfg.ws_base.replace("/ws", "/stream", 1)
    url = base + "?streams=" + "/".join(streams)
    delay = WS_BACKOFF_MIN
    last_event = 0.0
    while True:
        try:
            _ws_state(worker_id, status="connecting", ws_next_retry_at=None)
            async with websockets.connect(
                url, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT,
                close_timeout=5, open_timeout=15, max_size=2**24,
                max_queue=4096, compression=None,
            ) as ws:
                delay = WS_BACKOFF_MIN
                last_event = time.monotonic()
                _ws_state(worker_id, status="connected")
                web_runner.event("WS", f"Spot universe shard {worker_id} connected", symbols=len(symbols), streams=len(streams))
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_STALE_SECONDS)
                    except asyncio.TimeoutError as exc:
                        elapsed = time.monotonic() - last_event
                        raise ConnectionError(f"market data stale for {elapsed:.1f}s; forcing websocket reconnect") from exc
                    last_event = time.monotonic()
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        if data.get("s") and data.get("b") is not None and data.get("a") is not None:
                            if queue.full():
                                try:
                                    queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                            await queue.put(data)
                    except Exception as exc:
                        web_runner.event("WS_PARSE", str(exc), shard=worker_id)
        except asyncio.CancelledError:
            _ws_state(worker_id, status="stopped", ws_next_retry_at=None)
            raise
        except Exception as exc:
            now = time.time()
            _ws_state(worker_id, status="reconnecting", ws_disconnects=web_runner.STATE.get("ws_disconnects", 0) + 1, ws_last_disconnect=now)
            web_runner.event("WS_ERROR", f"Spot shard {worker_id} disconnected: {exc}", shard=worker_id)
            jitter = random.uniform(0.0, min(5.0, delay * 0.25))
            wait = min(WS_BACKOFF_MAX, delay + jitter)
            _ws_state(worker_id, ws_next_retry_at=time.time() + wait)
            web_runner.event("WS", f"Spot shard {worker_id} reconnect scheduled in {wait:.1f}s", shard=worker_id, retry_in=wait)
            await asyncio.sleep(wait)
            delay = min(WS_BACKOFF_MAX, delay * 2.0)
            _ws_state(worker_id, ws_reconnects=web_runner.STATE.get("ws_reconnects", 0) + 1)


async def _rest_book_ticker_worker(client, symbols, queue):
    """Keep scanning alive when a WS transport is silent or unavailable.

    Binance's all-symbol bookTicker endpoint is one public request and gives a
    current best bid/ask for every Spot symbol. It is a deliberately slower
    fallback, not a replacement for the low-latency WebSocket feed.
    """
    wanted = set(symbols)
    while True:
        try:
            payload = await asyncio.to_thread(client.book_ticker)
            count = 0
            now_ms = time.monotonic() * 1000
            for item in payload if isinstance(payload, list) else []:
                symbol = item.get("symbol")
                if symbol not in wanted:
                    continue
                bid = item.get("bidPrice")
                bid_qty = item.get("bidQty")
                ask = item.get("askPrice")
                ask_qty = item.get("askQty")
                if not all((bid, bid_qty, ask, ask_qty)):
                    continue
                data = {
                    "s": symbol,
                    "b": [[str(bid), str(bid_qty)]],
                    "a": [[str(ask), str(ask_qty)]],
                    "_source": "rest_book_ticker",
                    "_ts_ms": now_ms,
                }
                if queue.full():
                    break
                queue.put_nowait(data)
                count += 1
            with web_runner.LOCK:
                web_runner.STATE.setdefault("rest_fallback_updates", 0)
                web_runner.STATE["rest_fallback_updates"] += count
            web_runner.event("REST_SCAN", f"BookTicker fallback refreshed {count} symbols", symbols=count)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            web_runner.event("REST_SCAN_ERROR", f"BookTicker fallback failed: {exc}")
        await asyncio.sleep(REST_FALLBACK_SECONDS)


async def full_universe_stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    books = {}
    dirty = set()
    by_symbol = {}
    for i, triangle in enumerate(triangles):
        for symbol in triangle.symbols:
            by_symbol.setdefault(symbol, []).append(i)
    shards = [symbols[i:i + WS_SHARD_SIZE] for i in range(0, len(symbols), WS_SHARD_SIZE)]
    queue = asyncio.Queue(maxsize=20000)
    workers = [asyncio.create_task(_feed_worker(cfg, shard, queue, i + 1)) for i, shard in enumerate(shards)]
    # REST bookTicker is a bounded-rate safety net so the opportunity scanner
    # still runs if a WebSocket transport is accepted but emits no data.
    workers.append(asyncio.create_task(_rest_book_ticker_worker(client, symbols, queue)))
    with web_runner.LOCK:
        web_runner.STATE["ws_connected"] = bool(shards)
        web_runner.STATE["status"] = "running" if workers else "degraded"
        web_runner.STATE["symbols"] = len(symbols)
        web_runner.STATE["triangles"] = len(triangles)
        web_runner.STATE["ws_shards"] = len(shards)
        web_runner.STATE["ws_shard_size"] = WS_SHARD_SIZE
        web_runner.STATE.setdefault("rest_fallback_updates", 0)
    web_runner.event("UNIVERSE", f"Full Spot universe active: {len(symbols)} symbols, {len(triangles)} triangles, {len(shards)} WS shards", shard_size=WS_SHARD_SIZE)
    web_runner.event("SCAN", "Opportunity scanner armed with WebSocket + REST bookTicker fallback")

    last_order_ms = 0.0
    last_balance_ms = 0.0
    free_usdt = Decimal("0")
    balance_ok = False
    failures = 0
    try:
        while True:
            data = await queue.get()
            symbol = data["s"]
            bids = [(p, q) for p, q in data.get("b", [])[:cfg.depth_levels] if Decimal(str(p)) > 0 and Decimal(str(q)) > 0]
            asks = [(p, q) for p, q in data.get("a", [])[:cfg.depth_levels] if Decimal(str(q)) > 0 and Decimal(str(p)) > 0]
            if not bids or not asks:
                continue
            books[symbol] = {"bids": bids, "asks": asks, "depth_ts": time.monotonic() * 1000}
            dirty.update(by_symbol.get(symbol, ()))
            now = time.monotonic() * 1000
            candidates = list(dirty)
            dirty.clear()
            with web_runner.LOCK:
                web_runner.STATE["depth_updates"] += 1
                web_runner.STATE["scans"] += len(candidates)
                web_runner.STATE["last_scan"] = time.time()
            if not analysis_allowed():
                continue
            if now - last_balance_ms >= 1000:
                try:
                    account = await asyncio.to_thread(client.account)
                    free_usdt = next((Decimal(str(x.get("free", "0"))) for x in account.get("balances", []) if x.get("asset") == "USDT"), Decimal("0"))
                    last_balance_ms = now; balance_ok = True
                    with web_runner.LOCK:
                        web_runner.STATE["free_usdt"] = str(free_usdt)
                        web_runner.STATE["balance_refreshes"] += 1
                except Exception as exc:
                    balance_ok = False; last_balance_ms = now
                    web_runner.event("BALANCE_ERROR", f"balance refresh failed; trading paused until restored: {exc}")
            if not balance_ok:
                continue
            budget = web_runner.risk_budget(free_usdt, cfg.risk_pct, cfg.max_notional_usdt, Decimal(str(cfg.min_trade_notional_usdt)))
            if budget <= 0:
                with web_runner.LOCK:
                    web_runner.STATE["min_notional_blocks"] += 1
                continue
            for idx in candidates:
                triangle = triangles[idx]
                if not all(s in books and now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in triangle.symbols):
                    continue
                result = web_runner.evaluate_triangle(triangle, books, cfg.fee_bps, cfg.max_slippage_bps, symbol_meta, budget)
                if not result:
                    continue
                net_bps, gross_bps, path, first, second = result
                if net_bps < Decimal(str(cfg.min_net_edge_bps)):
                    continue
                with web_runner.LOCK:
                    web_runner.STATE["opportunities"] += 1; web_runner.STATE["last_opportunity"] = time.time()
                web_runner.event("OPPORTUNITY", f"net={net_bps:.3f} gross={gross_bps:.3f}", path=path, net_bps=float(net_bps), gross_bps=float(gross_bps))
                now_ms = time.monotonic() * 1000
                if not (cfg.live_trading and not cfg.dry_run) or not trading_allowed("spot") or now_ms - last_order_ms < cfg.cooldown_ms:
                    continue
                if not web_runner.approved(net_bps, cfg.min_net_edge_bps, budget, cfg.max_notional_usdt, min_trade_notional=Decimal(str(cfg.min_trade_notional_usdt))):
                    with web_runner.LOCK: web_runner.STATE["risk_blocks"] += 1
                    web_runner.event("RISK", "Trade blocked by risk/notional gate", path=path)
                    continue
                last_order_ms = now_ms
                try:
                    web_runner.event("LIVE", "Three-leg execution requested", path=path, net_bps=float(net_bps), budget=str(budget))
                    execution = await asyncio.to_thread(web_runner.execute_triangle, client, path, "USDT", first, budget, filters, False)
                    if not execution.get("finished") or execution.get("final_asset") != "USDT":
                        raise RuntimeError("execution returned without a completed USDT cycle")
                    web_runner.LEDGER.record(path, budget, execution)
                    with web_runner.LOCK:
                        web_runner.STATE["executions"] += 1; web_runner.STATE["last_execution"] = time.time()
                    web_runner._sync_ledger(); failures = 0
                    web_runner.event("FILLED", f"Triangle fully filled; realized={execution['realized_pnl_usdt']} USDT", path=path)
                except Exception as exc:
                    failures += 1
                    with web_runner.LOCK:
                        web_runner.STATE["execution_errors"] += 1; web_runner.STATE["last_error"] = str(exc)
                    if web_runner.LEDGER is not None: web_runner.LEDGER.record(path, budget, error=exc)
                    web_runner.event("ERROR", str(exc), path=path)
                    if failures >= 3:
                        raise RuntimeError("three consecutive execution failures; engine stopped for safety") from exc
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def run():
    web_runner.stream_loop = full_universe_stream_loop
    await web_runner.run()


def main():
    asyncio.run(main())

if __name__ == "__main__":
    main()
