from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.dex_0x import ZeroXAdapter
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine

STATE = {
    "status": "starting",
    "mode": "paper",
    "chain_id": None,
    "sources": [],
    "scans": 0,
    "opportunities": 0,
    "last_scan": None,
    "last_error": None,
    "started_at": time.time(),
}
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


def validate_evm_address(name: str, value: str) -> str:
    value = value.strip()
    if len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"{name} must be a 20-byte EVM address")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{name} contains non-hex characters") from exc
    return value


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    Thread(target=start_health_server, daemon=True).start()

    adapter = ZeroXAdapter()
    if not adapter.enabled:
        raise RuntimeError("ZEROX_API_KEY is required for the real DEX adapter")

    chain_id = int(os.getenv("DEX_CHAIN_ID", "8453"))
    taker = validate_evm_address("DEX_TAKER_ADDRESS", env_required("DEX_TAKER_ADDRESS"))
    quote_token = validate_evm_address("DEX_QUOTE_TOKEN", env_required("DEX_QUOTE_TOKEN"))
    base_token = validate_evm_address("DEX_BASE_TOKEN", env_required("DEX_BASE_TOKEN"))
    if quote_token.lower() == base_token.lower():
        raise ValueError("DEX_QUOTE_TOKEN and DEX_BASE_TOKEN must be different")

    quote_amount = int(os.getenv("DEX_QUOTE_AMOUNT_BASE_UNITS", "5000000"))
    quote_decimals = int(os.getenv("DEX_QUOTE_TOKEN_DECIMALS", "6"))
    min_profit = Decimal(os.getenv("DEX_MIN_NET_PROFIT", "0.005"))
    slippage_bps = int(os.getenv("DEX_SLIPPAGE_BPS", "50"))
    max_quote_latency_ms = Decimal(os.getenv("DEX_MAX_QUOTE_LATENCY_MS", "1000"))

    if quote_amount <= 0:
        raise ValueError("DEX_QUOTE_AMOUNT_BASE_UNITS must be positive")
    if min_profit < Decimal("0.005"):
        raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.005 USDT-equivalent")

    configured = tuple(x.strip() for x in os.getenv("DEX_SOURCES", "").split(",") if x.strip())
    if configured:
        sources = configured
    else:
        discovered = adapter.sources(chain_id)
        preferred = ("Uniswap_V3", "Aerodrome", "SushiSwap", "Uniswap_V2", "PancakeSwapV3")
        sources = tuple(x for x in preferred if x in discovered)[:4]

    if len(sources) < 2:
        raise RuntimeError(f"fewer than two usable DEX sources found on chain {chain_id}: {sources}")

    engine = DexCrossExchangeEngine(
        adapter,
        sources,
        min_profit=min_profit,
        quote_token_decimals=quote_decimals,
        max_quote_latency_ms=max_quote_latency_ms,
    )

    logging.info(
        "Dragon DEX cross-exchange | PAPER=%s | chain=%s sources=%s quote_amount=%s min_profit=%s",
        True,
        chain_id,
        sources,
        quote_amount,
        min_profit,
    )
    with LOCK:
        STATE["status"] = "running"
        STATE["chain_id"] = chain_id
        STATE["sources"] = list(sources)

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
                    logging.info(
                        "DEX OPPORTUNITY buy=%s sell=%s gross=%s net=%s",
                        best.buy_source,
                        best.sell_source,
                        best.gross_profit_quote,
                        best.net_profit_quote,
                    )
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
