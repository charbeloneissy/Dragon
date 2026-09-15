import asyncio
import json
import os
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import websockets

from src.dragon.binance import BinanceClient, BinanceError
from src.dragon.config import Config
from src.dragon.executor import execute_triangle
from src.dragon.risk import approved, risk_budget
from src.dragon.triangles import build_triangles, evaluate_triangle


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            body = b"dragon arbitrage runner alive\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    print(f"HEALTH SERVER | 0.0.0.0:{port}", flush=True)
    return server


def make_filters(info):
    out = {}
    for s in info.get("symbols", []):
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {})
        market_lot = fs.get("MARKET_LOT_SIZE", {})
        out[s["symbol"]] = {
            "baseAsset": s["baseAsset"],
            "quoteAsset": s["quoteAsset"],
            "stepSize": market_lot.get("stepSize", lot.get("stepSize", "0.00000001")),
            "minQty": market_lot.get("minQty", lot.get("minQty", "0")),
        }
    return out


async def stream_loop(cfg, client, filters, triangles, symbols):
    books = {}
    triangles_by_symbol = {}
    for triangle in triangles:
        for symbol in triangle.symbols:
            triangles_by_symbol.setdefault(symbol, []).append(triangle)

    last_order = 0.0
    failures = 0
    reconnect_delay = 1

    while True:
        try:
            async with websockets.connect(
                cfg.ws_base,
                ping_interval=10,
                ping_timeout=30,
                close_timeout=5,
                max_size=2**23,
            ) as ws:
                print("BINANCE WS CONNECTED", flush=True)
                for i in range(0, len(symbols), 200):
                    await ws.send(json.dumps({
                        "method": "SUBSCRIBE",
                        "params": [f"{s.lower()}@bookTicker" for s in symbols[i:i + 200]],
                        "id": i // 200 + 1,
                    }))
                reconnect_delay = 1

                while True:
                    msg = json.loads(await ws.recv())
                    if "s" not in msg or "b" not in msg or "a" not in msg:
                        continue

                    symbol = msg["s"]
                    books[symbol] = {
                        "bidPrice": msg["b"],
                        "bidQty": msg["B"],
                        "askPrice": msg["a"],
                        "askQty": msg["A"],
                        "ts": time.time() * 1000,
                    }
                    now = time.time() * 1000

                    for triangle in triangles_by_symbol.get(symbol, ()):
                        if not all(
                            s in books and now - books[s]["ts"] <= cfg.stale_ms
                            for s in triangle.symbols
                        ):
                            continue

                        result = evaluate_triangle(
                            triangle, books, cfg.fee_bps, cfg.slippage_bps
                        )
                        if not result:
                            continue

                        net_bps, gross_bps, path, first, second = result
                        if net_bps < Decimal(str(cfg.min_net_edge_bps)):
                            continue

                        print(
                            f"OPPORTUNITY | net_bps={net_bps:.3f} "
                            f"gross_bps={gross_bps:.3f} path={path}",
                            flush=True,
                        )

                        if not (cfg.live_trading and not cfg.dry_run):
                            continue
                        if now - last_order < cfg.cooldown_ms:
                            continue

                        try:
                            account = client.account()
                            free = next(
                                (
                                    Decimal(x["free"])
                                    for x in account.get("balances", [])
                                    if x["asset"] == "USDT"
                                ),
                                Decimal("0"),
                            )
                            budget = risk_budget(
                                free, cfg.risk_pct, cfg.max_notional_usdt
                            )
                            if not approved(
                                net_bps,
                                cfg.min_net_edge_bps,
                                budget,
                                cfg.max_notional_usdt,
                            ):
                                print("RISK BLOCK | opportunity or budget outside limits", flush=True)
                                continue
                            if budget <= 0:
                                print("RISK BLOCK | insufficient USDT budget", flush=True)
                                continue

                            print(
                                f"LIVE EXECUTION | budget_usdt={budget} path={path}",
                                flush=True,
                            )
                            execution = await asyncio.to_thread(
                                execute_triangle,
                                client,
                                path,
                                first,
                                second,
                                budget,
                                filters,
                                False,
                            )
                            print(f"EXECUTION COMPLETE | {execution}", flush=True)
                            failures = 0
                        except Exception as exc:
                            failures += 1
                            print(
                                f"EXECUTION ERROR | failures={failures} | {exc}",
                                flush=True,
                            )
                            if failures >= 3:
                                print(
                                    "CIRCUIT BREAKER | three consecutive execution failures",
                                    flush=True,
                                )
                                return
                        finally:
                            last_order = time.time() * 1000
        except Exception as exc:
            print(f"WS ERROR | reconnect_in={reconnect_delay}s | {exc}", flush=True)
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30)


async def run():
    cfg = Config.from_env()
    cfg.validate()
    start_health_server()
    client = BinanceClient(
        cfg.api_base,
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
    )
    info = client.exchange_info()
    filters = make_filters(info)
    triangles = build_triangles(info, cfg.max_triangles)
    symbols = sorted({s for t in triangles for s in t.symbols})
    print(
        f"DRAGON STARTED | triangles={len(triangles)} symbols={len(symbols)} "
        f"dry_run={cfg.dry_run} live={cfg.live_trading}",
        flush=True,
    )
    await stream_loop(cfg, client, filters, triangles, symbols)


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
