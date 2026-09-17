from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread
from decimal import Decimal

from src.dragon.dex_0x import ZeroXAdapter
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine

STATE = {"status": "starting", "mode": "paper", "scans": 0, "opportunities": 0, "last_scan": None, "last_error": None, "started_at": time.time()}
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            with LOCK:
                payload = dict(STATE)
            body = json.dumps(payload, default=str).encode()
            self.send_response(200 if payload["status"] in {"starting", "running", "degraded"} else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        return


def start_health_server() -> None:
    port = int(os.getenv("PORT", "10000"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    Thread(target=start_health_server, daemon=True).start()

    adapter = ZeroXAdapter()
    chain_id = int(os.getenv("DEX_CHAIN_ID", "8453"))
    taker = env_required("DEX_TAKER_ADDRESS")
    quote_token = env_required("DEX_QUOTE_TOKEN")
    base_token = env_required("DEX_BASE_TOKEN")
    quote_amount = int(os.getenv("DEX_QUOTE_AMOUNT_BASE_UNITS", "5000000"))
    min_profit = Decimal(os.getenv("DEX_MIN_NET_PROFIT", "0.005"))
    slippage_bps = int(os.getenv("DEX_SLIPPAGE_BPS", "50"))
    configured = tuple(x.strip() for x in os.getenv("DEX_SOURCES", "").split(",") if x.strip())
    if configured:
        sources = configured
    else:
        discovered = adapter.sources(chain_id)
        preferred = ("Uniswap_V3", "Aerodrome", "SushiSwap", "Uniswap_V2", "PancakeSwapV3")
        sources = tuple(x for x in preferred if x in discovered)[:4]
    if len(sources) < 2:
        raise RuntimeError(f"fewer than two usable DEX sources found on chain {chain_id}: {sources}")

    engine = DexCrossExchangeEngine(adapter, sources, min_profit=min_profit)
    logging.info("Dragon DEX cross-exchange | chain=%s sources=%s quote_amount=%s min_profit=%s", chain_id, sources, quote_amount, min_profit)
    with LOCK:
        STATE["status"] = "running"

    try:
        while True:
            try:
                opportunities = await asyncio.to_thread(
                    engine.scan_once,
                    chain_id=chain_id,
                    quote_token=quote_token,
                    base_token=base_token,
                    quote_amount=quote_amount,
                    taker=taker,
                    slippage_bps=slippage_bps,
                )
                with LOCK:
                    STATE["scans"] += 1
                    STATE["opportunities"] += len(opportunities)
                    STATE["last_scan"] = time.time()
                    STATE["last_error"] = None
                if opportunities:
                    best = opportunities[0]
                    logging.info("DEX OPPORTUNITY buy=%s sell=%s net_profit=%s", best.buy_source, best.sell_source, best.net_profit_quote)
                await asyncio.sleep(float(os.getenv("DEX_POLL_SECONDS", "0.5")))
            except Exception as exc:
                logging.exception("DEX scan failed")
                with LOCK:
                    STATE["status"] = "degraded"
                    STATE["last_error"] = str(exc)
                await asyncio.sleep(2)
                with LOCK:
                    STATE["status"] = "running"
    finally:
        adapter.close()


if __name__ == "__main__":
    asyncio.run(main())
