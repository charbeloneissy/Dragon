from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.dex_0x import ZeroXAdapter
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine

STATE = {
    "status": "starting", "mode": "paper", "chain_id": None, "sources": [],
    "scans": 0, "opportunities": 0, "last_scan": None, "last_error": None,
    "started_at": time.time(), "quote_amount": None, "quote_decimals": None,
    "min_net_profit": None, "safety_buffer": None, "own_capital": "0",
    "flash_liquidity": None, "flash_loan_enabled": False,
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


def env_decimal(name: str, default: str) -> Decimal:
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    Thread(target=start_health_server, daemon=True).start()

    adapter = ZeroXAdapter()
    try:
        if not adapter.enabled:
            raise RuntimeError("ZEROX_API_KEY is required for the real DEX adapter")

        chain_id = int(os.getenv("DEX_CHAIN_ID", "8453"))
        if chain_id <= 0:
            raise ValueError("DEX_CHAIN_ID must be positive")

        taker = validate_evm_address("DEX_TAKER_ADDRESS", env_required("DEX_TAKER_ADDRESS"))
        quote_token = validate_evm_address("DEX_QUOTE_TOKEN", env_required("DEX_QUOTE_TOKEN"))
        base_token = validate_evm_address("DEX_BASE_TOKEN", env_required("DEX_BASE_TOKEN"))
        if quote_token.lower() == base_token.lower():
            raise ValueError("DEX_QUOTE_TOKEN and DEX_BASE_TOKEN must be different")

        quote_decimals = int(os.getenv("DEX_QUOTE_TOKEN_DECIMALS", "6"))
        if not 0 <= quote_decimals <= 36:
            raise ValueError("DEX_QUOTE_TOKEN_DECIMALS must be between 0 and 36")

        own_capital = env_decimal("DEX_OWN_CAPITAL_QUOTE", "0")
        if own_capital != 0:
            raise ValueError("DEX_OWN_CAPITAL_QUOTE must remain exactly 0")

        flash_liquidity = env_decimal("DEX_FLASH_LOAN_LIQUIDITY_QUOTE", "0")
        if flash_liquidity <= 0:
            raise RuntimeError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE must be provided by the flash-liquidity provider")

        min_profit = env_decimal("DEX_MIN_NET_PROFIT", "0.005")
        if min_profit < Decimal("0.005"):
            raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.005 USDT-equivalent")
        safety_buffer = env_decimal("DEX_SAFETY_BUFFER", "0.001")
        if safety_buffer < 0:
            raise ValueError("DEX_SAFETY_BUFFER cannot be negative")

        slippage_bps = int(os.getenv("DEX_SLIPPAGE_BPS", "50"))
        max_quote_latency_ms = env_decimal("DEX_MAX_QUOTE_LATENCY_MS", "1000")
        if not 0 <= slippage_bps <= 5000:
            raise ValueError("DEX_SLIPPAGE_BPS must be between 0 and 5000")
        if max_quote_latency_ms <= 0:
            raise ValueError("DEX_MAX_QUOTE_LATENCY_MS must be positive")

        flash_loan_enabled = env_bool("FLASH_LOAN_ENABLED", False)
        flash_loan_fee_bps = env_decimal("FLASH_LOAN_FEE_BPS", "0")
        if flash_loan_fee_bps < 0 or flash_loan_fee_bps > 1000:
            raise ValueError("FLASH_LOAN_FEE_BPS must be between 0 and 1000")
        if env_bool("MEV_PROTECTION_REQUIRED", True) and env_bool("LIVE_TRADING", False):
            raise RuntimeError("live execution requires an implemented MEV-protected executor")
        if env_bool("ATOMIC_REPAYMENT_REQUIRED", True) and env_bool("LIVE_TRADING", False):
            raise RuntimeError("live execution requires an implemented atomic flash-loan repayment path")

        configured = tuple(x.strip() for x in os.getenv("DEX_SOURCES", "").split(",") if x.strip())
        sources = configured or tuple(x for x in ("Uniswap_V3", "Aerodrome", "SushiSwap", "Uniswap_V2", "PancakeSwapV3") if x in adapter.sources(chain_id))[:4]
        if len(sources) < 2:
            raise RuntimeError(f"fewer than two usable DEX sources found on chain {chain_id}: {sources}")

        engine = DexCrossExchangeEngine(
            adapter, sources, min_profit=min_profit, quote_token_decimals=quote_decimals,
            max_quote_latency_ms=max_quote_latency_ms, safety_buffer_quote=safety_buffer,
            flash_loan_enabled=flash_loan_enabled, flash_loan_fee_bps=flash_loan_fee_bps,
        )

        with LOCK:
            STATE.update({"status": "running", "chain_id": chain_id, "sources": list(sources),
                          "quote_amount": None, "quote_decimals": quote_decimals,
                          "min_net_profit": str(min_profit), "safety_buffer": str(safety_buffer),
                          "own_capital": str(own_capital), "flash_liquidity": str(flash_liquidity),
                          "flash_loan_enabled": flash_loan_enabled})

        logging.info("Dragon DEX cross-exchange | PAPER=True | chain=%s sources=%s own_capital=%s flash_liquidity=%s min_net=%s safety=%s",
                     chain_id, sources, own_capital, flash_liquidity, min_profit, safety_buffer)

        while True:
            try:
                opportunities = await asyncio.to_thread(
                    engine.scan_max_profitable, chain_id=chain_id, quote_token=quote_token,
                    base_token=base_token, max_quote_amount=flash_liquidity,
                    taker=taker, slippage_bps=slippage_bps,
                )
                with LOCK:
                    STATE["scans"] += 1
                    STATE["opportunities"] += len(opportunities)
                    STATE["last_scan"] = time.time()
                    STATE["last_error"] = None
                    STATE["quote_amount"] = str(opportunities[0].quote_amount / (Decimal(10) ** quote_decimals)) if opportunities else None
                if opportunities:
                    best = opportunities[0]
                    logging.info("DEX OPPORTUNITY size=%s buy=%s sell=%s gross=%s gas=%s flash_fee=%s safety=%s net=%s",
                                 best.quote_amount / (Decimal(10) ** quote_decimals), best.buy_source, best.sell_source,
                                 best.gross_profit_quote, best.gas_cost_quote, best.flash_loan_fee_quote,
                                 best.safety_buffer_quote, best.net_profit_quote)
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
