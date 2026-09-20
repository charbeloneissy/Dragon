from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.chains import get_spec, rpc_urls_for
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine
from src.dragon.dex_evm import RpcRateLimitError
from src.dragon.dex_multichain import MultiChainDexAdapter
from src.dragon.observability import ExecutionTelemetry
from src.dragon.rpc_providers import load_providers, provider_status
from src.dragon.universe import universe_payload
from src.dragon.venues import venues_for

STATE = {
    "status": "starting", "mode": "paper", "chains": [], "chain_details": {},
    "sources": [], "scans": 0, "opportunities": 0, "last_scan": None,
    "last_error": None, "started_at": time.time(), "quote_decimals": None,
    "min_net_profit": None, "safety_buffer": None, "own_capital": "0",
    "flash_liquidity": None, "flash_cap": None, "flash_loan_enabled": False,
    "compounding_enabled": False, "compound_amount_quote": "0",
    "compound_reserve_quote": "0", "last_tx_hash": None, "rejections": {},
    "base_tokens": {}, "universe_mode": {}, "universe": [], "opportunity_records": [],
    "rpc_providers": {}, "rpc_hosts": {},
    "data_source": "multi-chain cross-DEX executable quotes (EVM + non-EVM)",
}
LOCK = Lock()
METRICS = ExecutionTelemetry()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        with LOCK:
            payload = dict(STATE)
            payload["rejections"] = dict(STATE["rejections"])
            payload["observability"] = METRICS.snapshot()
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return
        if path == "/dashboard":
            try:
                with open("dashboard.html", "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError as exc:
                body = json.dumps({"error": "dashboard unavailable", "detail": str(exc)}).encode()
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return
        if path in ("/", "/health", "/healthz", "/api/status"):
            body = json.dumps(payload, default=str).encode()
            self.send_response(200)
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


class DragonHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = DragonHTTPServer(("0.0.0.0", port), Handler)
    logging.info("Dragon HTTP server listening on 0.0.0.0:%s", port)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def env_decimal(name, default):
    try:
        value = Decimal(os.getenv(name, default))
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def env_bool(name, default=False):
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def rpc_host(url):
    """Host of an RPC URL with any embedded key/path stripped, for safe logging."""
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc
    except Exception:
        return "unknown"


def quote_units(human_amount: Decimal, decimals: int) -> int:
    """Convert a human quote amount into ERC-20 base units."""
    if not human_amount.is_finite() or human_amount <= 0:
        raise ValueError("quote amount must be positive and finite")
    if not 0 <= decimals <= 36:
        raise ValueError("quote decimals must be between 0 and 36")
    units = int(human_amount * (Decimal(10) ** decimals))
    if units <= 0:
        raise ValueError("quote amount is below one token base unit")
    return units


def human_quote_amount(raw_amount: int, decimals: int) -> Decimal:
    return Decimal(raw_amount) / (Decimal(10) ** decimals)


def validate_evm_address(name, value):
    value = value.strip()
    if len(value) != 42 or not value.startswith("0x"):
        raise ValueError(f"{name} must be a 20-byte EVM address")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"{name} contains non-hex characters") from exc
    return value


# Default base-token universe per EVM chain: wrapped native plus a few deep assets.
PAPER_BASE_TOKENS: dict[int, tuple[str, ...]] = {
    1: ("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",),
    10: ("0x4200000000000000000000000000000000000006",),
    56: ("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",),
    130: ("0x4200000000000000000000000000000000000006",),
    137: ("0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",),
    324: ("0x5AEa5775959fBC2557Cc8789bC1bf90A239D9a91",),
    480: ("0x4200000000000000000000000000000000000006",),
    5000: ("0x78c1b0C915c4FAA5FffA6CAbf0219DA63d7f4cb8",),
    8453: ("0x4200000000000000000000000000000000000006",),
    42161: ("0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",),
    42220: ("0x471EcE3750Da237f93B8E339c536989b8978a438",),
    43114: ("0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",),
    59144: ("0xe5D7C2a44FfddF6b295A15c148167daaAf5CF34f",),
    534352: ("0x5300000000000000000000000000000000000004",),
    81457: ("0x4300000000000000000000000000000000000004",),
}

# Default quote token (stablecoin) per EVM chain. Overridable via DEX_QUOTE_TOKENS.
DEFAULT_QUOTE_TOKENS: dict[int, tuple[str, int]] = {
    1: ("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6),
    10: ("0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6),
    56: ("0x55d398326f99059fF775485246999027B3197955", 18),
    130: ("0x078D782b760474a361dDA0AF3839290b0EF57AD6", 6),
    137: ("0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6),
    324: ("0x3355df6D4c9C3035724Fd0e3914dE96A5a83aaf4", 6),
    480: ("0x79A02482A880bCE3F13e09Da970dC34db4CD24d1", 6),
    5000: ("0x09Bc4E0D864854c6aFB6eB9A9cdF58aC190D0dF9", 6),
    8453: ("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6),
    42161: ("0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6),
    42220: ("0xcebA9300f2b948710d2653dD7B07f33A8B32118C", 6),
    43114: ("0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", 6),
    59144: ("0x176211869cA2b568f2A7D4EE941E073a821EE1ff", 6),
    534352: ("0x06eFdBFf2a14a7c8E15944D1F4A48F9F95F663A4", 6),
    81457: ("0x4300000000000000000000000000000000000003", 18),
}

# Non-EVM chains: (chain, venue, sell_denom, buy_denom).
NONEVM_PROBES: dict[str, tuple[str, str, str]] = {
    "tron": ("SunSwap_V2", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "TSSMHYeV2uE9qYH95DqyoCuNCzEL1NvU3S"),
    "cosmos": ("Osmosis", "uosmo", "ibc/27394FB092D2ECCD56123C74F36E4C1F926001CEADA9CA97EA622B25F41E5EB2"),
    "solana": ("Jupiter", "So11111111111111111111111111111111111111112", "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"),
}


def _enabled_evm_chains() -> list[int]:
    raw = os.getenv("DEX_CHAINS", "8453").strip()
    ids: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cid = int(part)
        except ValueError:
            continue
        if get_spec(cid) is not None and cid not in ids:
            ids.append(cid)
    return ids or [8453]


def _enabled_nonevm() -> list[str]:
    raw = os.getenv("NONEVM_CHAINS", "").strip()
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _quote_token_for(chain_id: int) -> tuple[str, int]:
    configured = os.getenv("DEX_QUOTE_TOKENS", "").strip()
    if configured:
        for entry in configured.split(","):
            parts = entry.split(":")
            if len(parts) == 3 and parts[0].strip() == str(chain_id):
                return validate_evm_address("DEX_QUOTE_TOKENS", parts[1]), int(parts[2])
    default = DEFAULT_QUOTE_TOKENS.get(chain_id)
    if default is None:
        raise ValueError(f"no default quote token for chain {chain_id}; set DEX_QUOTE_TOKENS")
    return default


def _base_tokens_for(chain_id: int) -> list[str]:
    configured = os.getenv("DEX_PAPER_BASE_TOKENS", "").strip()
    if configured:
        tokens = [validate_evm_address("DEX_PAPER_BASE_TOKENS", t) for t in configured.split(",") if t.strip()]
        if tokens:
            return tokens
    return list(PAPER_BASE_TOKENS.get(chain_id, ()))


def merge_rejections(stats):
    with LOCK:
        for key, value in stats.items():
            STATE["rejections"][key] = int(value)


def opportunity_view(opportunity, identifier, chain_label, quote_decimals):
    return {
        "id": identifier,
        "chain": chain_label,
        "buy_source": opportunity.buy_source,
        "sell_source": opportunity.sell_source,
        "base_token": opportunity.base_token,
        "quote_token": opportunity.quote_token,
        "quote_amount": str(opportunity.quote_amount),
        "quote_amount_human": str(human_quote_amount(opportunity.quote_amount, quote_decimals)),
        "gross_profit_quote": str(opportunity.gross_profit_quote),
        "net_profit_quote": str(opportunity.net_profit_quote),
        "gas_cost_quote": str(opportunity.gas_cost_quote),
        "flash_loan_fee_quote": str(opportunity.flash_loan_fee_quote),
        "safety_buffer_quote": str(opportunity.safety_buffer_quote),
        "status": "ready_for_fresh_simulation",
    }


async def to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def scan_evm_chain(adapter, chain_id, *, max_quote, taker, slippage, min_profit):
    """Scan every venue pair on one EVM chain for one base token."""
    spec = get_spec(chain_id)
    quote_token, quote_decimals = _quote_token_for(chain_id)
    base_tokens = _base_tokens_for(chain_id)
    venue_names = [v.name for v in venues_for(chain_id)]
    if len(venue_names) < 2:
        return [], {}
    engine = DexCrossExchangeEngine(
        adapter, venue_names, min_profit=min_profit, quote_token_decimals=quote_decimals,
        flash_loan_enabled=env_bool("FLASH_LOAN_ENABLED", False),
        flash_loan_fee_bps=env_decimal("FLASH_LOAN_FEE_BPS", "0"),
        telemetry=METRICS,
    )
    found: list = []
    for base_token in base_tokens:
        try:
            opportunities = await to_thread(
                engine.scan_max_profitable,
                chain_id=chain_id, quote_token=quote_token, base_token=base_token,
                max_quote_amount=max_quote, taker=taker, slippage_bps=slippage,
            )
            found.extend(opportunities)
        except Exception as exc:
            logging.warning("scan failed chain=%s base=%s error=%s: %s", spec.name, base_token, type(exc).__name__, exc)
    return found, dict(engine.last_rejections)


async def scan_nonevm_chain(adapter, chain, *, slippage):
    """Probe the configured non-EVM venues and report gross spreads."""
    probe = NONEVM_PROBES.get(chain)
    if not probe:
        return [], {}
    venue, sell_denom, buy_denom = probe
    try:
        quote, _ = await to_thread(
            adapter.quote_unified,
            chain=chain, venue=venue, sell_token=sell_denom, buy_token=buy_denom,
            sell_amount=10**6, slippage_bps=slippage, probe=True,
        )
        METRICS.increment("nonevm_quotes")
        logging.info("non-EVM quote chain=%s venue=%s out=%s", chain, venue, quote.buy_amount)
        return [], {}
    except Exception as exc:
        logging.warning("non-EVM probe failed chain=%s venue=%s error=%s: %s", chain, venue, type(exc).__name__, exc)
        return [], {f"nonevm_{chain}_error": 1}


async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    start_health_server()
    adapter = None
    try:
        logging.info("Dragon multi-chain scanner boot")
        retry = 1.0
        while adapter is None:
            try:
                adapter = MultiChainDexAdapter()
            except RpcRateLimitError as exc:
                logging.warning("DEX adapters unavailable; retrying in %.1fs: %s", retry, exc)
                await asyncio.sleep(retry)
                retry = min(15.0, retry * 2.0)

        evm_chains = _enabled_evm_chains()
        nonevm_chains = _enabled_nonevm()
        min_profit = env_decimal("DEX_MIN_NET_PROFIT", "0.0025")
        if min_profit < Decimal("0.0025"):
            raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.0025")
        safety = env_decimal("DEX_SAFETY_BUFFER", "0.001")
        slippage = int(os.getenv("DEX_SLIPPAGE_BPS", "50"))
        flash_cap_quote = env_decimal("DEX_FLASH_LOAN_LIQUIDITY_QUOTE", "10000")
        if flash_cap_quote <= 0:
            raise ValueError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE must be positive")
        taker = os.getenv("DEX_TAKER_ADDRESS", "").strip() or "0x000000000000000000000000000000000000dEaD"
        poll = float(os.getenv("DEX_POLL_SECONDS", "2.0"))

        chain_details = {}
        for cid in evm_chains:
            spec = get_spec(cid)
            quote_token, quote_decimals = _quote_token_for(cid)
            chain_details[spec.name] = {
                "chain_id": cid,
                "family": "evm",
                "venues": list(adapter.sources(cid)),
                "quote_token": quote_token,
                "quote_decimals": quote_decimals,
                "base_tokens": _base_tokens_for(cid),
                "flash_scan_cap_quote": str(flash_cap_quote),
                "flash_scan_cap_raw": str(quote_units(flash_cap_quote, quote_decimals)),
            }
        for family in nonevm_chains:
            chain_details[family] = {
                "chain_id": family,
                "family": "non-evm",
                "venues": list(adapter.sources(family)),
            }
        with LOCK:
            STATE.update({
                "status": "running",
                "mode": "live" if env_bool("LIVE_TRADING", False) else "paper",
                "chains": list(chain_details.keys()),
                "chain_details": chain_details,
                "universe": universe_payload(chain_details),
                "sources": sorted({v for d in chain_details.values() for v in d["venues"]}),
                "min_net_profit": str(min_profit),
                "safety_buffer": str(safety),
                "flash_cap": str(flash_cap_quote),
                "flash_cap_unit": "human quote units",
                "flash_loan_enabled": env_bool("FLASH_LOAN_ENABLED", False),
                "compounding_enabled": env_bool("DEX_COMPOUND_PROFITS", False),
                "rpc_providers": provider_status(load_providers()),
                "rpc_hosts": {
                    get_spec(cid).name: [rpc_host(u) for u in rpc_urls_for(cid)]
                    for cid in evm_chains
                },
            })
        if not any(provider_status(load_providers()).values()):
            logging.warning(
                "No private RPC keys detected (ALCHEMY_API_KEY / INFURA_API_KEY / QUICKNODE_*); "
                "using public endpoints, which are rate-limited."
            )
        total_venues = sum(len(d["venues"]) for d in chain_details.values())
        logging.info("Dragon multi-chain ready chains=%s venues=%s min_profit=%s", list(chain_details.keys()), total_venues, min_profit)

        while True:
            try:
                all_found = []
                rejections = {}
                for cid in evm_chains:
                    quote_token, quote_decimals = _quote_token_for(cid)
                    chain_flash_cap = quote_units(flash_cap_quote, quote_decimals)
                    found, rej = await scan_evm_chain(
                        adapter, cid, max_quote=chain_flash_cap, taker=taker,
                        slippage=slippage, min_profit=min_profit,
                    )
                    for opp in found:
                        all_found.append((opp, get_spec(cid).name))
                    for k, v in rej.items():
                        rejections[k] = rejections.get(k, 0) + int(v)
                for family in nonevm_chains:
                    _, rej = await scan_nonevm_chain(adapter, family, slippage=slippage)
                    for k, v in rej.items():
                        rejections[k] = rejections.get(k, 0) + int(v)
                merge_rejections(rejections)
                all_found.sort(key=lambda pair: pair[0].net_profit_quote, reverse=True)
                top = all_found[:max(1, int(os.getenv("DEX_MAX_OPPORTUNITIES", "8")))]
                rows = []
                for index, (opp, chain_label) in enumerate(top):
                    identifier = f"{STATE['scans']}-{index}"
                    try:
                        METRICS.record_opportunity(opp)
                    except Exception:
                        logging.debug("telemetry record_opportunity failed", exc_info=True)
                    quote_decimals = int(next((d["quote_decimals"] for d in chain_details.values() if d.get("chain_id") == opp.chain_id), 6))
                    rows.append(opportunity_view(opp, identifier, chain_label, quote_decimals))
                opportunities = top
                with LOCK:
                    STATE["scans"] += 1
                    STATE["opportunities"] += len(opportunities)
                    STATE["last_scan"] = time.time()
                    STATE["last_error"] = None
                    STATE["opportunity_records"] = rows
                logging.info("scan complete chains=%s opportunities=%s rejections=%s", len(evm_chains), len(opportunities), rejections)
                await asyncio.sleep(poll)
            except Exception as exc:
                logging.exception("scan/execution failed")
                with LOCK:
                    STATE["status"] = "degraded"
                    STATE["last_error"] = str(exc)
                await asyncio.sleep(2)
                with LOCK:
                    STATE["status"] = "running"
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                logging.exception("failed to close DEX adapter")


if __name__ == "__main__":
    asyncio.run(main())
