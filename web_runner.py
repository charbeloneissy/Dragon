import asyncio
import json
import os
import time
from decimal import Decimal

import websockets

from src.dragon.binance import BinanceClient, BinanceError
from src.dragon.config import Config
from src.dragon.risk import approved, risk_budget
from src.dragon.triangles import build_triangles, evaluate_triangle


async def run():
    cfg = Config.from_env()
    cfg.validate()
    key = os.getenv("BINANCE_API_KEY", "")
    secret = os.getenv("BINANCE_API_SECRET", "")
    client = BinanceClient(cfg.api_base, key, secret)
    info = client.exchange_info()
    triangles = build_triangles(info, cfg.max_triangles)
    symbols = sorted({s for t in triangles for s in t.symbols})
    print(f"DRAGON STARTED | triangles={len(triangles)} symbols={len(symbols)} dry_run={cfg.dry_run} live={cfg.live_trading}", flush=True)

    books = {}
    last_order = 0.0
    async with websockets.connect(cfg.ws_base, ping_interval=20, ping_timeout=20, max_size=2**23) as ws:
        for i in range(0, len(symbols), 200):
            params = [f"{s.lower()}@bookTicker" for s in symbols[i:i+200]]
            await ws.send(json.dumps({"method":"SUBSCRIBE","params":params,"id":i//200+1}))
        while True:
            msg = json.loads(await ws.recv())
            if "s" not in msg or "b" not in msg or "a" not in msg:
                continue
            books[msg["s"]] = {"bidPrice": msg["b"], "bidQty": msg["B"], "askPrice": msg["a"], "askQty": msg["A"], "ts": time.time() * 1000}
            now_ms = time.time() * 1000
            for t in triangles:
                if any(now_ms - books[s]["ts"] > cfg.stale_ms for s in t.symbols if s in books):
                    continue
                result = evaluate_triangle(t, books, cfg.fee_bps, cfg.slippage_bps)
                if not result:
                    continue
                net_bps, gross_bps, path, first, second = result
                if not approved(net_bps, cfg.min_net_edge_bps, Decimal(str(cfg.max_notional_usdt)), cfg.max_notional_usdt):
                    continue
                print(f"OPPORTUNITY | net_bps={net_bps:.3f} gross_bps={gross_bps:.3f} path={path}", flush=True)
                if cfg.live_trading and not cfg.dry_run and (time.time()*1000-last_order) >= cfg.cooldown_ms:
                    try:
                        account = client.account()
                        free = next((Decimal(x["free"]) for x in account["balances"] if x["asset"] == "USDT"), Decimal("0"))
                        budget = risk_budget(free, cfg.risk_pct, cfg.max_notional_usdt)
                        if budget <= 0:
                            continue
                        # Execution remains deliberately gated to the dedicated executor.
                        print(f"LIVE GATE PASSED | budget_usdt={budget}", flush=True)
                    except BinanceError as e:
                        print(f"BINANCE PRIVATE AUTH FAILED | {e}", flush=True)
                    last_order = time.time()*1000


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
