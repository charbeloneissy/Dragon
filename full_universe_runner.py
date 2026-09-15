import asyncio
import json
import time
from decimal import Decimal

import websockets

import web_runner
from src.dragon.control import analysis_allowed, trading_allowed


async def _feed_worker(cfg, symbols, queue, worker_id):
    """Maintain one combined Binance Spot stream for a symbol shard."""
    streams = [f"{s.lower()}@depth{cfg.depth_levels}@100ms" for s in symbols]
    if not streams:
        return
    url = cfg.ws_base.replace("/ws", "/stream", 1) + "?streams=" + "/".join(streams)
    delay = 1
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=10, close_timeout=5, max_size=2**23) as ws:
                web_runner.event("WS", f"Market-data shard {worker_id} connected", symbols=len(symbols))
                delay = 1
                async for raw in ws:
                    try:
                        msg = json.loads(raw); data = msg.get("data", msg)
                        if data.get("s") and data.get("b") is not None and data.get("a") is not None:
                            await queue.put(data)
                    except Exception as exc:
                        web_runner.event("WS_PARSE", str(exc), shard=worker_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            web_runner.event("WS_ERROR", str(exc), shard=worker_id)
            await asyncio.sleep(delay); delay = min(delay * 2, 15)


async def full_universe_stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    """Scan the full Spot triangle universe using sharded combined streams."""
    books = {}; dirty = set(); by_symbol = {}
    for i, triangle in enumerate(triangles):
        for symbol in triangle.symbols: by_symbol.setdefault(symbol, []).append(i)
    shard_size = 900
    shards = [symbols[i:i + shard_size] for i in range(0, len(symbols), shard_size)]
    queue = asyncio.Queue(maxsize=20000)
    workers = [asyncio.create_task(_feed_worker(cfg, shard, queue, i + 1)) for i, shard in enumerate(shards)]
    with web_runner.LOCK:
        web_runner.STATE["ws_connected"] = True; web_runner.STATE["status"] = "running"
        web_runner.STATE["symbols"] = len(symbols); web_runner.STATE["triangles"] = len(triangles)
    web_runner.event("UNIVERSE", f"Full Spot triangle universe active: {len(symbols)} symbols, {len(triangles)} triangles, {len(shards)} WS shards")
    last_order_ms = 0.0; last_balance_ms = 0.0; free_usdt = Decimal("0"); failures = 0
    try:
        while True:
            data = await queue.get(); symbol = data["s"]
            bids = [(p, q) for p, q in data.get("b", [])[:cfg.depth_levels] if Decimal(str(p)) > 0 and Decimal(str(q)) > 0]
            asks = [(p, q) for p, q in data.get("a", [])[:cfg.depth_levels] if Decimal(str(p)) > 0 and Decimal(str(q)) > 0]
            if not bids or not asks: continue
            books[symbol] = {"bids": bids, "asks": asks, "depth_ts": time.monotonic() * 1000}; dirty.update(by_symbol.get(symbol, ()))
            now = time.monotonic() * 1000
            candidates = list(dirty); dirty.clear()
            with web_runner.LOCK:
                web_runner.STATE["depth_updates"] += 1; web_runner.STATE["scans"] += len(candidates)
            if not analysis_allowed():
                continue
            for idx in candidates:
                triangle = triangles[idx]
                if not all(s in books and now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in triangle.symbols): continue
                if now - last_balance_ms > 1000:
                    try:
                        account = await asyncio.to_thread(client.account)
                        free_usdt = next((Decimal(x["free"]) for x in account.get("balances", []) if x.get("asset") == "USDT"), Decimal("0"))
                        last_balance_ms = now
                        with web_runner.LOCK: web_runner.STATE["free_usdt"] = str(free_usdt)
                    except Exception as exc:
                        web_runner.event("BALANCE_ERROR", str(exc)); continue
                budget = web_runner.risk_budget(free_usdt, cfg.risk_pct, cfg.max_notional_usdt)
                if budget <= 0: continue
                result = web_runner.evaluate_triangle(triangle, books, cfg.fee_bps, cfg.max_slippage_bps, symbol_meta, budget)
                if not result: continue
                net_bps, gross_bps, path, first, second = result
                if net_bps < Decimal(str(cfg.min_net_edge_bps)): continue
                with web_runner.LOCK:
                    web_runner.STATE["opportunities"] += 1; web_runner.STATE["last_opportunity"] = time.time()
                web_runner.event("OPPORTUNITY", f"net={net_bps:.3f} gross={gross_bps:.3f}", path=path, net_bps=float(net_bps), gross_bps=float(gross_bps))
                now_ms = time.monotonic() * 1000
                if not (cfg.live_trading and not cfg.dry_run) or not trading_allowed("spot") or now_ms - last_order_ms < cfg.cooldown_ms:
                    continue
                if not web_runner.approved(net_bps, cfg.min_net_edge_bps, budget, cfg.max_notional_usdt):
                    with web_runner.LOCK: web_runner.STATE["risk_blocks"] += 1
                    web_runner.event("RISK", "Trade blocked by risk/notional gate", path=path); continue
                last_order_ms = now_ms
                try:
                    web_runner.event("LIVE", "Three-leg execution requested", path=path, net_bps=float(net_bps), budget=str(budget))
                    execution = await asyncio.to_thread(web_runner.execute_triangle, client, path, "USDT", first, budget, filters, False)
                    if not execution.get("finished") or execution.get("final_asset") != "USDT": raise RuntimeError("execution returned without a completed USDT cycle")
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
                    if failures >= 3: raise RuntimeError("three consecutive execution failures; engine stopped for safety") from exc
    finally:
        for task in workers: task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


async def run():
    web_runner.stream_loop = full_universe_stream_loop
    await web_runner.run()


def main(): asyncio.run(run())

if __name__ == "__main__": main()
