import asyncio
import json
import os
import random
import time
from decimal import Decimal

import websockets

import web_runner
from src.dragon.control import analysis_allowed, trading_allowed
from src.dragon.multi_exchange import MultiExchangeFeeds
from src.dragon.universe import classify_triangle

WS_BACKOFF_MIN = 1.0
WS_BACKOFF_MAX = 60.0
WS_STALE_SECONDS = 45.0
WS_SHARD_SIZE = 100
WS_PING_INTERVAL = 20.0
WS_PING_TIMEOUT = 20.0
REST_FALLBACK_SECONDS = 3.0
DISPLAY_MIN_NET_BPS = Decimal(os.getenv("DASHBOARD_MIN_NET_EDGE_BPS", "0"))
DISPLAY_MAX_NET_BPS = Decimal(os.getenv("DASHBOARD_MAX_NET_EDGE_BPS", "100000"))\n

def _ws_state(worker_id, **values):
    with web_runner.LOCK:
        for key, value in values.items(): web_runner.STATE[key] = value
        web_runner.STATE.setdefault("ws_reconnects", 0); web_runner.STATE.setdefault("ws_disconnects", 0)
        web_runner.STATE.setdefault("ws_last_disconnect", None); web_runner.STATE.setdefault("ws_next_retry_at", None)
        web_runner.STATE.setdefault("ws_shard_status", {})
        status = dict(web_runner.STATE["ws_shard_status"])
        status[str(worker_id)] = values.get("status", status.get(str(worker_id), "unknown"))
        web_runner.STATE["ws_shard_status"] = status


async def _feed_worker(cfg, symbols, queue, worker_id):
    streams = [f"{s.lower()}@depth{cfg.depth_levels}@100ms" for s in symbols]
    if not streams: return
    base = cfg.ws_base.replace("/ws", "/stream", 1); url = base + "?streams=" + "/".join(streams); delay = WS_BACKOFF_MIN
    last_event = 0.0
    while True:
        try:
            _ws_state(worker_id, status="connecting", ws_next_retry_at=None)
            async with websockets.connect(url, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT, close_timeout=5, open_timeout=15, max_size=2**24, max_queue=4096, compression=None) as ws:
                delay = WS_BACKOFF_MIN; last_event = time.monotonic(); _ws_state(worker_id, status="connected")
                web_runner.event("WS", f"Spot universe shard {worker_id} connected", symbols=len(symbols), streams=len(streams))
                while True:
                    try: raw = await asyncio.wait_for(ws.recv(), timeout=WS_STALE_SECONDS)
                    except asyncio.TimeoutError as exc:
                        elapsed = time.monotonic() - last_event; raise ConnectionError(f"market data stale for {elapsed:.1f}s; forcing websocket reconnect") from exc
                    last_event = time.monotonic()
                    try:
                        msg = json.loads(raw); data = msg.get("data", msg)
                        if data.get("s") and data.get("b") is not None and data.get("a") is not None:
                            if queue.full():
                                try: queue.get_nowait()
                                except asyncio.QueueEmpty: pass
                            await queue.put(data)
                    except Exception as exc: web_runner.event("WS_PARSE", str(exc), shard=worker_id)
        except asyncio.CancelledError:
            _ws_state(worker_id, status="stopped", ws_next_retry_at=None); raise
        except Exception as exc:
            now = time.time(); _ws_state(worker_id, status="reconnecting", ws_disconnects=web_runner.STATE.get("ws_disconnects", 0) + 1, ws_last_disconnect=now)
            web_runner.event("WS_ERROR", f"Spot shard {worker_id} disconnected: {exc}", shard=worker_id)
            jitter = random.uniform(0.0, min(5.0, delay * 0.25)); wait = min(WS_BACKOFF_MAX, delay + jitter)
            _ws_state(worker_id, ws_next_retry_at=time.time() + wait); web_runner.event("WS", f"Spot shard {worker_id} reconnect scheduled in {wait:.1f}s", shard=worker_id, retry_in=wait)
            await asyncio.sleep(wait); delay = min(WS_BACKOFF_MAX, delay * 2.0); _ws_state(worker_id, ws_reconnects=web_runner.STATE.get("ws_reconnects", 0) + 1)


async def _rest_book_ticker_worker(client, symbols, queue):
    wanted = set(symbols)
    while True:
        try:
            payload = await asyncio.to_thread(client.book_ticker); count = 0; now_ms = time.monotonic() * 1000
            for item in payload if isinstance(payload, list) else []:
                symbol = item.get("symbol")
                if symbol not in wanted: continue
                bid, bid_qty, ask, ask_qty = item.get("bidPrice"), item.get("bidQty"), item.get("askPrice"), item.get("askQty")
                if not all((bid, bid_qty, ask, ask_qty)): continue
                data = {"s": symbol, "b": [[str(bid), str(bid_qty)]], "a": [[str(ask), str(ask_qty)]], "_source": "rest_book_ticker", "_ts_ms": now_ms}
                if queue.full(): break
                queue.put_nowait(data); count += 1
            with web_runner.LOCK:
                web_runner.STATE.setdefault("rest_fallback_updates", 0); web_runner.STATE["rest_fallback_updates"] += count
            web_runner.event("REST_SCAN", f"BookTicker fallback refreshed {count} symbols", symbols=count)
        except asyncio.CancelledError: raise
        except Exception as exc: web_runner.event("REST_SCAN_ERROR", f"BookTicker fallback failed: {exc}")
        await asyncio.sleep(REST_FALLBACK_SECONDS)


async def full_universe_stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    books = {}; dirty = set(); by_symbol = {}
    for i, triangle in enumerate(triangles):
        for symbol in triangle.symbols: by_symbol.setdefault(symbol, []).append(i)
    shards = [symbols[i:i + WS_SHARD_SIZE] for i in range(0, len(symbols), WS_SHARD_SIZE)]; queue = asyncio.Queue(maxsize=20000)
    workers = [asyncio.create_task(_feed_worker(cfg, shard, queue, i + 1)) for i, shard in enumerate(shards)]
    workers.append(asyncio.create_task(_rest_book_ticker_worker(client, symbols, queue)))
    external = MultiExchangeFeeds(web_runner.STATE, web_runner.LOCK, web_runner.event, symbols=symbols)
    external_task = asyncio.create_task(external.run())
    with web_runner.LOCK:
        web_runner.STATE["ws_connected"] = bool(shards); web_runner.STATE["status"] = "running" if workers else "degraded"
        web_runner.STATE["symbols"] = len(symbols); web_runner.STATE["triangles"] = len(triangles); web_runner.STATE["ws_shards"] = len(shards); web_runner.STATE["ws_shard_size"] = WS_SHARD_SIZE
        web_runner.STATE.setdefault("rest_fallback_updates", 0); web_runner.STATE.setdefault("cross_exchange_opportunities", [])
        web_runner.STATE["universe_mode"] = cfg.universe_mode
        web_runner.STATE["decision_cycle_seconds"] = cfg.decision_cycle_seconds
        web_runner.STATE["universe_scanned"] = len(triangles)
        web_runner.STATE["universe_qualified"] = 0
        web_runner.STATE["universe_execution_ready"] = 0
        web_runner.STATE["universe_rejected"] = 0
        web_runner.STATE["universe_selected"] = 0
        web_runner.STATE["universe_max_entries"] = 0
        web_runner.STATE["universe_rejection_reasons"] = {}
        web_runner.STATE["universe_top"] = []
    web_runner.event("UNIVERSE", f"Full dynamic Spot universe active: {len(symbols)} symbols, {len(triangles)} triangles, {len(shards)} WS shards", shard_size=WS_SHARD_SIZE)
    web_runner.event("SCAN", f"Scanner armed; continuous WS + {cfg.decision_cycle_seconds}s decision cycle; net-edge threshold={cfg.min_net_edge_bps:g}bps")

    last_order_ms = 0.0; last_balance_ms = 0.0; free_usdt = Decimal("0"); balance_ok = False; failures = 0
    try:
        while True:
            data = await queue.get(); batch = [data]
            for _ in range(min(queue.qsize(), 1000)):
                try: batch.append(queue.get_nowait())
                except asyncio.QueueEmpty: break
            for item in batch:
                symbol = item.get("s")
                if not symbol: continue
                bids = [(p, q) for p, q in item.get("b", [])[:cfg.depth_levels] if Decimal(str(p)) > 0 and Decimal(str(q)) > 0]
                asks = [(p, q) for p, q in item.get("a", [])[:cfg.depth_levels] if Decimal(str(q)) > 0 and Decimal(str(p)) > 0]
                if not bids or not asks: continue
                books[symbol] = {"bids": bids, "asks": asks, "depth_ts": time.monotonic() * 1000}; dirty.update(by_symbol.get(symbol, ()))
            now = time.monotonic() * 1000; candidates = list(dirty); dirty.clear()
            with web_runner.LOCK:
                web_runner.STATE["depth_updates"] += len(batch); web_runner.STATE["scans"] += len(candidates); web_runner.STATE["last_scan"] = time.time()
            if not analysis_allowed(): continue
            if now - last_balance_ms >= 1000:
                try:
                    account = await asyncio.to_thread(client.account)
                    free_usdt = next((Decimal(str(x.get("free", "0"))) for x in account.get("balances", []) if x.get("asset") == "USDT"), Decimal("0")); last_balance_ms = now; balance_ok = True
                    with web_runner.LOCK: web_runner.STATE["free_usdt"] = str(free_usdt); web_runner.STATE["balance_refreshes"] += 1
                except Exception as exc:
                    balance_ok = False; last_balance_ms = now; web_runner.event("BALANCE_ERROR", f"balance refresh failed; trading paused until restored: {exc}")
            if not balance_ok or free_usdt <= 0: continue

            # Use the same reserve/allocation budget for scanning, qualification,
            # and execution. Never turn raw free USDT into an executable budget.
            trade_budget = web_runner.risk_budget(
                free_usdt,
                cfg.risk_pct,
                cfg.max_notional_usdt,
                Decimal(str(cfg.min_trade_notional_usdt)),
                capital_allocation_pct=cfg.capital_allocation_pct,
                safety_reserve_usdt=Decimal(str(cfg.safety_reserve_usdt)),
            )
            evaluation_notional = trade_budget
            max_entries = int(trade_budget / Decimal(str(cfg.min_trade_notional_usdt))) if trade_budget > 0 else 0
            with web_runner.LOCK:
                web_runner.STATE["universe_max_entries"] = max_entries
                web_runner.STATE["trade_budget"] = str(trade_budget)

            if trade_budget <= 0:
                with web_runner.LOCK:
                    web_runner.STATE["min_notional_blocks"] += 1
                continue

            cycle_start = time.monotonic(); cycle_top = []; cycle_qualified = 0; cycle_ready = 0; cycle_rejected = 0; cycle_reasons = {}
            for idx in candidates:
                triangle = triangles[idx]
                if not all(s in books and now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in triangle.symbols): continue
                result = web_runner.evaluate_triangle(triangle, books, cfg.fee_bps, cfg.max_slippage_bps, symbol_meta, evaluation_notional)
                if not result: continue
                net_bps, gross_bps, path, first, second = result
                expected_profit = evaluation_notional * net_bps / Decimal("10000")
                edge_ok = net_bps >= Decimal(str(cfg.min_net_edge_bps))
                decision = classify_triangle(
                    triangle, books, symbol_meta, now, evaluation_notional,
                    stale_ms=cfg.stale_ms,
                    max_slippage_bps=Decimal(str(cfg.max_slippage_bps)),
                    net_edge_bps=net_bps,
                    min_net_edge_bps=Decimal(str(cfg.min_net_edge_bps)),
                    expected_profit_usdt=expected_profit,
                    min_expected_profit_usdt=Decimal(str(cfg.min_expected_profit_usdt)),
                )
                eligible = edge_ok and decision.qualified
                ready = edge_ok and decision.execution_ready and trade_budget >= Decimal(str(cfg.min_trade_notional_usdt)) and max_entries > 0
                if eligible: cycle_qualified += 1
                if ready: cycle_ready += 1
                if not ready:
                    cycle_rejected += 1
                    reason = decision.rejection or ("NET_EDGE" if not edge_ok else "CAPITAL")
                    cycle_reasons[reason] = cycle_reasons.get(reason, 0) + 1
                cycle_top.append({"path": path, "tier": decision.tier, "score": float(decision.score), "net_bps": float(net_bps), "gross_bps": float(gross_bps), "liquidity_factor": float(decision.liquidity_factor), "expected_profit_usdt": str(expected_profit), "qualified": eligible, "execution_ready": ready, "rejection": decision.rejection})

                web_runner.record_opportunity(path, net_bps, gross_bps, evaluation_notional, eligible=eligible, trade_budget=trade_budget)
                gate = "PASS" if ready else (decision.rejection or ("NET_EDGE" if not edge_ok else "CAPITAL"))
                web_runner.event("CANDIDATE", f"tier={decision.tier} score={decision.score:.3f} net={net_bps:.3f} gross={gross_bps:.3f} expected=${expected_profit:.6f} gate={gate}", path=path, tier=decision.tier, score=float(decision.score), net_bps=float(net_bps), gross_bps=float(gross_bps), expected_profit_usdt=str(expected_profit), evaluation_notional=str(evaluation_notional), trade_budget=str(trade_budget), rejection_reason=gate)

                if not ready: continue
                with web_runner.LOCK: web_runner.STATE["opportunities"] += 1; web_runner.STATE["last_opportunity"] = time.time()
                web_runner.event("OPPORTUNITY", f"tier={decision.tier} score={decision.score:.3f} net={net_bps:.3f} gross={gross_bps:.3f} expected=${expected_profit:.6f}", path=path, net_bps=float(net_bps), gross_bps=float(gross_bps), expected_profit_usdt=str(expected_profit), evaluation_notional=str(evaluation_notional), universe_score=float(decision.score))

                now_ms = time.monotonic() * 1000
                if not (cfg.live_trading and not cfg.dry_run) or not trading_allowed("spot") or now_ms - last_order_ms < cfg.cooldown_ms: continue
                if not web_runner.approved(net_bps, cfg.min_net_edge_bps, trade_budget, cfg.max_notional_usdt, min_trade_notional=Decimal(str(cfg.min_trade_notional_usdt))):
                    with web_runner.LOCK: web_runner.STATE["risk_blocks"] += 1
                    web_runner.event("RISK", "Trade blocked by final edge/notional gate", path=path); continue
                last_order_ms = now_ms
                try:
                    web_runner.event("LIVE", "Three-leg execution requested", path=path, net_bps=float(net_bps), budget=str(trade_budget), expected_profit_usdt=str(expected_profit))
                    execution = await asyncio.to_thread(web_runner.execute_triangle, client, path, "USDT", first, trade_budget, filters, False)
                    if not execution.get("finished") or execution.get("final_asset") != "USDT": raise RuntimeError("execution returned without a completed USDT cycle")
                    web_runner.LEDGER.record(path, trade_budget, execution)
                    with web_runner.LOCK: web_runner.STATE["executions"] += 1; web_runner.STATE["last_execution"] = time.time()
                    web_runner._sync_ledger(); failures = 0
                    web_runner.event("FILLED", f"Triangle fully filled; realized={execution['realized_pnl_usdt']} USDT", path=path)
                except Exception as exc:
                    failures += 1
                    with web_runner.LOCK: web_runner.STATE["execution_errors"] += 1; web_runner.STATE["last_error"] = str(exc)
                    if web_runner.LEDGER is not None: web_runner.LEDGER.record(path, trade_budget, error=exc)
                    web_runner.event("ERROR", str(exc), path=path)
                    if failures >= 3: raise RuntimeError("three consecutive execution failures; engine stopped for safety") from exc

            if cycle_top:
                cycle_top.sort(key=lambda x: x["score"], reverse=True)
                cycle_top = cycle_top[:20]
            with web_runner.LOCK:
                web_runner.STATE["universe_qualified"] = cycle_qualified
                web_runner.STATE["universe_execution_ready"] = cycle_ready
                web_runner.STATE["universe_rejected"] = cycle_rejected
                web_runner.STATE["universe_rejection_reasons"] = cycle_reasons
                web_runner.STATE["universe_top"] = cycle_top
                web_runner.STATE["universe_last_cycle_at"] = time.time()
                web_runner.STATE["universe_cycle_ms"] = round((time.monotonic() - cycle_start) * 1000, 2)
                web_runner.STATE["universe_selected"] = min(cycle_ready, max_entries)
    finally:
        external_task.cancel()
        for task in workers: task.cancel()
        await asyncio.gather(external_task, *workers, return_exceptions=True)
