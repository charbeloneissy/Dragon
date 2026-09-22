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
from src.dragon.triangular import TriangularEngine
from src.dragon.gas_sponsor import GasSponsorManager
from src.dragon.execution_datacenter import ExecutionCapacity
from src.dragon.dex_evm import RpcRateLimitError
from src.dragon.dex_multichain import MultiChainDexAdapter
from src.dragon.observability import ExecutionTelemetry
from src.dragon.rpc_providers import load_providers, provider_status
from src.dragon.admin_server import start_admin_server
from src.dragon.universe import universe_payload
from src.dragon.venues import venues_for
from src.dragon.hash_utils import opportunity_hash
from src.dragon.aave_mcp import AaveMCPClient, DEFAULT_STABLECOINS, fetch_best_stablecoin_yields, parse_markets
from src.dragon.aave_supply_yield import find_enterable_supply_candidates
from src.dragon.aave_graphql import AAVE_V4_ARC_CHAIN_ID, AaveGraphQLClient
from src.dragon.aave_agent import AAVE_AGENT_SOURCES, AAVE_AGENT_WORKFLOW, AaveAgentPolicy
from src.dragon.aave_address_book import snapshot as aave_address_snapshot
from src.dragon.aave_stable_vault import load_validated_stable_vaults
from src.dragon.aave_flash import env_flash_loan_config, validate_config as validate_flash_loan_config
from src.dragon.aave_umbrella import AaveUmbrellaClient, choose_umbrella_candidates
from src.dragon.aerodrome_opportunity import AerodromeOpportunityEngine, snapshot_from_dict
from src.dragon.economic_agent import EconomicDecisionAgent
from src.dragon.dragon_core import DragonCore, ChainState, EconomicCandidate, ThreeBrainEngines
from src.dragon.base_live import BaseLiveReader
from src.dragon.five_circle_engine import FiveCircleEngine
from src.dragon.base_proof_engine import BaseProofEngine
from src.dragon.base_atomic_simulator import BaseAtomicSimulator

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
    "aave_mcp_enabled": False, "aave_mcp_last_update": None,
    "aave_mcp_error": None, "aave_stable_yields": [], "aave_stable_markets": 0,
    "aave_user_state_enabled": False, "aave_user_state_wallet": None,
    "aave_user_state_last_update": None, "aave_user_state_error": None,
    "aave_user_positions": [], "aave_user_summary": None, "aave_user_rewards": None,
    "aave_supply_yield_enabled": False, "aave_supply_yield_last_update": None,
    "aave_supply_yield_error": None, "aave_supply_candidates": [],
    "aave_supply_yield_wallet": None,
    "aave_umbrella_enabled": False, "aave_umbrella_last_update": None,
    "aave_umbrella_error": None, "aave_umbrella_wallet": None,
    "aave_umbrella_positions": [], "aave_umbrella_candidates": [],
    "aave_stable_vaults_enabled": False, "aave_stable_vaults": [], "aave_stable_vault_error": None,
    "aave_v4_enabled": False, "aave_v4_chains": [], "aave_v4_arc": None, "aave_v4_last_update": None,
    "aave_v4_error": None, "aave_v4_liquidity": {"spokes": [], "reserves": []}, "aave_v4_liquidity_error": None,
    "aave_horizon_enabled": False, "aave_horizon_last_update": None,
    "aave_horizon_error": None, "aave_horizon_market": None,
    "aave_agent": {"workflow": AAVE_AGENT_WORKFLOW, "sources": AAVE_AGENT_SOURCES, "policy": "discover-inspect-simulate-build-wallet-sign-confirm"},
    "aerodrome": {"enabled": True, "mode": "read_only", "deployment_allowed": False, "opportunities": [], "last_update": None, "error": None},
    "aave_address_book": aave_address_snapshot(),
    "data_source": "multi-chain multi-scanner executable quotes + triangular/cross-DEX + Aave MCP read-only market data",
    "triangular_enabled": False, "triangular_opportunities": 0,
    "execution_capacity": 8, "gas_sponsor_enabled": False, "gas_sponsor_required": False,
    "economic_agent": {"enabled": True, "last_update": None, "ranked_orders": [], "chain_memory": {}},
    "dragon_core": {},
    "brain_engines": {},
    "base_live": {},
    "five_circle": {"rotation": 0, "status": "waiting", "selected": None, "decisions": []},
}
LOCK = Lock()
METRICS = ExecutionTelemetry()
AERODROME_ENGINE = AerodromeOpportunityEngine()

def refresh_aerodrome_opportunities() -> None:
    """Evaluate configured Aerodrome snapshots; never deploy capital."""
    raw = os.getenv("AERODROME_POOLS_JSON", "").strip()
    if not raw:
        with LOCK:
            STATE["aerodrome"].update({"enabled": True, "mode": "read_only", "deployment_allowed": False, "opportunities": [], "last_update": time.time(), "error": None})
        return
    try:
        payload = json.loads(raw)
        if not isinstance(payload, list):
            raise ValueError("AERODROME_POOLS_JSON must be a JSON array")
        pools = [snapshot_from_dict(item) for item in payload if isinstance(item, dict)]
        rows = AERODROME_ENGINE.rank(pools)
        with LOCK:
            STATE["aerodrome"].update({"enabled": True, "mode": "read_only", "deployment_allowed": False, "opportunities": rows[:20], "last_update": time.time(), "error": None})
    except Exception as exc:
        logging.warning("Aerodrome opportunity evaluation failed: %s", exc)
        with LOCK:
            STATE["aerodrome"].update({"enabled": True, "mode": "read_only", "deployment_allowed": False, "opportunities": [], "last_update": time.time(), "error": str(exc)})


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
        if path == "/api/aave/agent":
            body = json.dumps({
                "workflow": payload.get("aave_agent", {}).get("workflow"),
                "sources": payload.get("aave_agent", {}).get("sources", {}),
                "policy": payload.get("aave_agent", {}).get("policy"),
                "address_book": payload.get("aave_address_book", {}),
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/aave/v4/liquidity":
            body = json.dumps({
                "enabled": payload.get("aave_v4_enabled", False),
                "error": payload.get("aave_v4_liquidity_error"),
                "liquidity": payload.get("aave_v4_liquidity", {"spokes": [], "reserves": []}),
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/aave/user-state":
            body = json.dumps({
                "enabled": payload.get("aave_user_state_enabled", False),
                "wallet": payload.get("aave_user_state_wallet"),
                "last_update": payload.get("aave_user_state_last_update"),
                "error": payload.get("aave_user_state_error"),
                "positions": payload.get("aave_user_positions", []),
                "summary": payload.get("aave_user_summary"),
                "rewards": payload.get("aave_user_rewards"),
                "unsigned_only": True,
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/aave/horizon":
            body = json.dumps({
                "enabled": payload.get("aave_horizon_enabled", False),
                "last_update": payload.get("aave_horizon_last_update"),
                "error": payload.get("aave_horizon_error"),
                "market": payload.get("aave_horizon_market"),
                "unsigned_only": True,
                "market_address": "0xAe05Cd22df81871bc7cC2a04BeCfb516bFe332C8",
                "chain_id": 1,
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/aave/v4/chains":
            body = json.dumps({
                "enabled": payload.get("aave_v4_enabled", False),
                "last_update": payload.get("aave_v4_last_update"),
                "error": payload.get("aave_v4_error"),
                "chains": payload.get("aave_v4_chains", []),
                "arc": payload.get("aave_v4_arc"),
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/aave/stable-vaults":
            body = json.dumps({
                "enabled": payload.get("aave_stable_vaults_enabled", False),
                "error": payload.get("aave_stable_vault_error"),
                "vaults": payload.get("aave_stable_vaults", []),
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/aave/supply-yields":
            body = json.dumps({
                "enabled": payload.get("aave_supply_yield_enabled", False),
                "wallet": payload.get("aave_supply_yield_wallet"),
                "last_update": payload.get("aave_supply_yield_last_update"),
                "error": payload.get("aave_supply_yield_error"),
                "candidates": payload.get("aave_supply_candidates", []),
                "unsigned_only": True,
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/aave/umbrella":
            body = json.dumps({
                "enabled": payload.get("aave_umbrella_enabled", False),
                "wallet": payload.get("aave_umbrella_wallet"),
                "last_update": payload.get("aave_umbrella_last_update"),
                "error": payload.get("aave_umbrella_error"),
                "positions": payload.get("aave_umbrella_positions", []),
                "candidates": payload.get("aave_umbrella_candidates", []),
                "unsigned_only": True,
                "network": "Ethereum",
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/api/aave/yields":
            body = json.dumps({
                "enabled": payload.get("aave_mcp_enabled", False),
                "last_update": payload.get("aave_mcp_last_update"),
                "error": payload.get("aave_mcp_error"),
                "markets": payload.get("aave_stable_markets", 0),
                "yields": payload.get("aave_stable_yields", []),
            }, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
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


def configured_base_venues(chain_id: int) -> list[str]:
    """Return the exact DEX allowlist for the cross-DEX scanner."""
    configured = [
        x.strip()
        for x in os.getenv("DRAGON_BASE_VENUES", "Aerodrome,Uniswap_V3").split(",")
        if x.strip()
    ]
    available = {v.name for v in venues_for(chain_id)}
    selected = [name for name in configured if name in available]
    unknown = [name for name in configured if name not in available]
    if unknown:
        logging.warning(
            "Ignoring unknown/unavailable Base DEX venues chain=%s venues=%s",
            chain_id,
            unknown,
        )
    if len(selected) < 2:
        raise ValueError(
            f"DRAGON_BASE_VENUES must resolve to at least two available venues on chain {chain_id}; "
            f"configured={configured} available={sorted(available)}"
        )
    return selected


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
    raw = os.getenv("DEX_CHAINS", "1,56,43114,8453,42161,10,137,130,324,7777777,480,42220,59144,534352,81457,5000").strip()
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


def _validate_quote_token_config(chain_id: int) -> tuple[str, int]:
    """Validate quote-token configuration without allowing one bad chain to crash Dragon."""
    token, decimals = _quote_token_for(chain_id)
    if not 0 <= decimals <= 36:
        raise ValueError(f"quote decimals out of range for chain {chain_id}: {decimals}")
    validate_evm_address(f"quote token for chain {chain_id}", token)
    if not _base_tokens_for(chain_id):
        raise ValueError(f"no base tokens configured for chain {chain_id}")
    return token, decimals


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


def _triangular_tokens_for(chain_id: int) -> tuple[str, str, str] | None:
    """Return an operator-verified three-token cycle for a chain."""
    raw = os.getenv("DEX_TRIANGULAR_TOKENS", "").strip()
    if not raw: return None
    for entry in raw.split(","):
        parts = [p.strip() for p in entry.split(":")]
        if len(parts) != 4 or parts[0] != str(chain_id): continue
        try:
            tokens = tuple(validate_evm_address(f"triangular token {i}", p) for i, p in enumerate(parts[1:], 1))
        except ValueError: continue
        if len(set(t.lower() for t in tokens)) != 3: continue
        return tokens
    return None

async def scan_triangular_chain(adapter, chain_id, *, max_quote, min_profit):
    if not env_bool("TRIANGULAR_ARBITRAGE_ENABLED", True): return [], {}
    cycle = _triangular_tokens_for(chain_id)
    if cycle is None: return [], {"triangular_cycle_not_configured": 1}
    configured_venues = [x.strip() for x in os.getenv("DRAGON_BASE_VENUES", "").split(",") if x.strip()]
    venue_names = [v.name for v in venues_for(chain_id) if not configured_venues or v.name in configured_venues]
    if not venue_names: return [], {"triangular_no_venues": 1}
    engine = TriangularEngine(adapter, min_profit=min_profit, quote_decimals=int(_quote_token_for(chain_id)[1]),
                              flash_fee_bps=env_decimal("FLASH_LOAN_FEE_BPS", "0"),
                              max_workers=int(os.getenv("DEX_MAX_LEG_WORKERS", "24")))
    routes = engine.discover_routes(chain_id, venue_names, cycle)
    found = await to_thread(engine.optimize, routes, max_quote)
    return found, {}

def triangular_view(opportunity, identifier, chain_label, quote_decimals):
    return {"id": identifier, "strategy": "triangular", "chain": chain_label,
            "venues": list(opportunity.route.venues),
            "tokens": [opportunity.route.token_a, opportunity.route.token_b, opportunity.route.token_c],
            "quote_amount": str(opportunity.quote_amount),
            "quote_amount_human": str(human_quote_amount(opportunity.quote_amount, quote_decimals)),
            "final_amount": str(opportunity.final_amount),
            "gross_profit_quote": str(opportunity.gross_profit_quote),
            "net_profit_quote": str(opportunity.net_profit_quote),
            "gas_cost_quote": str(opportunity.gas_cost_quote),
            "flash_loan_fee_quote": str(opportunity.flash_loan_fee_quote),
            "status": "ready_for_fresh_simulation"}
def merge_rejections(stats):
    with LOCK:
        for key, value in stats.items():
            STATE["rejections"][key] = int(value)

async def refresh_aave_v4_liquidity():
    """Refresh Arc/V4 Spoke + Reserve liquidity off the latency-critical quote path."""
    enabled = env_bool("AAVE_V4_LIQUIDITY_ENABLED", True)
    interval = max(30.0, float(os.getenv("AAVE_V4_LIQUIDITY_REFRESH_SECONDS", "60")))
    chain_id = int(os.getenv("AAVE_V4_LIQUIDITY_CHAIN_ID", str(AAVE_V4_ARC_CHAIN_ID)))
    client = AaveGraphQLClient()
    if not enabled:
        return

    while True:
        try:
            spokes = await client.spokes({"query": {"chainIds": [chain_id]}})
            reserves = await client.reserves({"query": {"chainIds": [chain_id]}})
            with LOCK:
                STATE["aave_v4_liquidity"] = {
                    "spokes": spokes,
                    "reserves": reserves,
                }
                STATE["aave_v4_liquidity_error"] = None
            logging.info(
                "AaveKit V4 liquidity refreshed chain=%s spokes=%s reserves=%s",
                chain_id, len(spokes), len(reserves),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_v4_liquidity_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning(
                "AaveKit V4 liquidity refresh failed: %s: %s",
                type(exc).__name__, exc,
            )
        await asyncio.sleep(interval)


async def refresh_aave_horizon():
    """Refresh the Aave Horizon V3 market off the latency-critical DEX path."""
    enabled = env_bool("AAVE_HORIZON_ENABLED", True)
    interval = max(30.0, float(os.getenv("AAVE_HORIZON_REFRESH_SECONDS", "60")))
    client = AaveGraphQLClient()
    with LOCK:
        STATE["aave_horizon_enabled"] = enabled
    if not enabled:
        return

    while True:
        try:
            market = await client.horizon_market()
            if not isinstance(market, dict):
                raise RuntimeError("Aave Horizon GraphQL returned no market")
            with LOCK:
                STATE["aave_horizon_market"] = market
                STATE["aave_horizon_last_update"] = time.time()
                STATE["aave_horizon_error"] = None
            reserves = market.get("reserves") or []
            logging.info(
                "Aave Horizon refreshed reserves=%s liquidity=%s",
                len(reserves),
                market.get("totalAvailableLiquidity"),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_horizon_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning(
                "Aave Horizon refresh failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        await asyncio.sleep(interval)


async def refresh_aave_v4():
    """Refresh AaveKit v4 chain metadata without blocking DEX scans."""
    enabled = env_bool("AAVE_V4_GRAPHQL_ENABLED", True)
    interval = max(30.0, float(os.getenv("AAVE_V4_GRAPHQL_REFRESH_SECONDS", "60")))
    client = AaveGraphQLClient()
    with LOCK:
        STATE["aave_v4_enabled"] = enabled
    if not enabled:
        return

    while True:
        try:
            rows = await client.chains()
            arc = next(
                (row for row in rows if int(row.get("chainId", -1)) == AAVE_V4_ARC_CHAIN_ID),
                None,
            )
            with LOCK:
                STATE["aave_v4_chains"] = rows
                STATE["aave_v4_arc"] = arc
                STATE["aave_v4_last_update"] = time.time()
                STATE["aave_v4_error"] = None
            logging.info("AaveKit v4 refreshed chains=%s arc=%s", len(rows), bool(arc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_v4_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning("AaveKit v4 refresh failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)


async def refresh_aave_supply_yield():
    """Refresh Aave V4 supply opportunities for yield parking/compounding."""
    enabled = env_bool("AAVE_YIELD_ENABLED", False)
    interval = max(30.0, float(os.getenv("AAVE_YIELD_REFRESH_SECONDS", "60")))
    wallet = os.getenv("AAVE_YIELD_WALLET_ADDRESS", "").strip() or None
    stablecoins = tuple(
        x.strip().upper()
        for x in os.getenv("AAVE_YIELD_STABLECOINS", ",".join(DEFAULT_STABLECOINS)).split(",")
        if x.strip()
    ) or DEFAULT_STABLECOINS
    client = AaveMCPClient()

    with LOCK:
        STATE["aave_supply_yield_enabled"] = enabled
        STATE["aave_supply_yield_wallet"] = wallet
    if not enabled:
        return

    while True:
        try:
            payload = await client.get_markets(
                version="v4",
                symbols=list(stablecoins),
                user=wallet,
            )
            rows = parse_markets(payload, stablecoins=stablecoins)
            candidates = [candidate.as_dict() for candidate in find_enterable_supply_candidates(rows)]
            with LOCK:
                STATE["aave_supply_candidates"] = candidates
                STATE["aave_supply_yield_last_update"] = time.time()
                STATE["aave_supply_yield_error"] = None
            logging.info(
                "Aave V4 supply-yield refresh candidates=%s wallet_state=%s",
                len(candidates),
                bool(wallet),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_supply_yield_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning("Aave V4 supply-yield refresh failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)


async def refresh_aave_umbrella():
    """Refresh Umbrella stake/reward state off the 500 ms DEX path."""
    enabled = env_bool("AAVE_UMBRELLA_ENABLED", False)
    interval = max(60.0, float(os.getenv("AAVE_UMBRELLA_REFRESH_SECONDS", "300")))
    wallet = (
        os.getenv("AAVE_UMBRELLA_WALLET_ADDRESS", "").strip()
        or os.getenv("AAVE_YIELD_WALLET_ADDRESS", "").strip()
        or None
    )
    with LOCK:
        STATE["aave_umbrella_enabled"] = enabled
        STATE["aave_umbrella_wallet"] = wallet
    if not enabled or not wallet:
        return
    rpc_candidates = rpc_urls_for(1)
    if not rpc_candidates:
        with LOCK:
            STATE["aave_umbrella_error"] = "no Ethereum RPC configured"
        return
    client = AaveUmbrellaClient(rpc_url=rpc_candidates[0])
    while True:
        try:
            snapshots = await to_thread(client.snapshot, wallet)
            candidates = [item.as_dict() for item in choose_umbrella_candidates(snapshots)]
            positions = [item.as_dict() for item in snapshots if item.user_shares > 0]
            with LOCK:
                STATE["aave_umbrella_positions"] = positions
                STATE["aave_umbrella_candidates"] = candidates
                STATE["aave_umbrella_last_update"] = time.time()
                STATE["aave_umbrella_error"] = None
            logging.info(
                "Aave Umbrella refreshed positions=%s candidates=%s",
                len(positions),
                len(candidates),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_umbrella_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning(
                "Aave Umbrella refresh failed: %s: %s",
                type(exc).__name__,
                exc,
            )
        await asyncio.sleep(interval)


async def refresh_aave_user_state():
    """Refresh wallet positions/summary/rewards outside the DEX quote path."""
    enabled = env_bool("AAVE_USER_STATE_ENABLED", False)
    interval = max(30.0, float(os.getenv("AAVE_USER_STATE_REFRESH_SECONDS", "60")))
    wallet = os.getenv("AAVE_USER_STATE_WALLET_ADDRESS", "").strip() or os.getenv("AAVE_YIELD_WALLET_ADDRESS", "").strip() or None
    client = AaveMCPClient()

    with LOCK:
        STATE["aave_user_state_enabled"] = enabled
        STATE["aave_user_state_wallet"] = wallet
    if not enabled or not wallet:
        return

    while True:
        try:
            positions, summary, rewards = await asyncio.gather(
                client.get_user_positions(user=wallet, version="all"),
                client.get_user_summary(user=wallet, version="all"),
                client.get_user_rewards(user=wallet, version="all"),
            )
            with LOCK:
                STATE["aave_user_positions"] = positions
                STATE["aave_user_summary"] = summary
                STATE["aave_user_rewards"] = rewards
                STATE["aave_user_state_last_update"] = time.time()
                STATE["aave_user_state_error"] = None
            logging.info("Aave user state refreshed wallet=%s", wallet[:10])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_user_state_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning("Aave user state refresh failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)


async def refresh_aave_mcp():
    """Refresh Aave V3/V4 stablecoin market data without blocking DEX scans."""
    enabled = env_bool("AAVE_MCP_ENABLED", True)
    stablecoins = tuple(
        x.strip().upper()
        for x in os.getenv("AAVE_MCP_STABLECOINS", ",".join(DEFAULT_STABLECOINS)).split(",")
        if x.strip()
    ) or DEFAULT_STABLECOINS
    interval = max(30.0, float(os.getenv("AAVE_MCP_REFRESH_SECONDS", "60")))
    client = AaveMCPClient()

    with LOCK:
        STATE["aave_mcp_enabled"] = enabled
    if not enabled:
        return

    while True:
        try:
            rows = await fetch_best_stablecoin_yields(
                client=client,
                stablecoins=stablecoins,
                limit=max(1, min(50, int(os.getenv("AAVE_MCP_TOP_N", "12")))),
            )
            with LOCK:
                STATE["aave_stable_yields"] = rows
                STATE["aave_stable_markets"] = len(rows)
                STATE["aave_mcp_last_update"] = time.time()
                STATE["aave_mcp_error"] = None
            logging.info("Aave MCP refreshed stablecoin markets=%s", len(rows))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with LOCK:
                STATE["aave_mcp_error"] = f"{type(exc).__name__}: {exc}"
            logging.warning("Aave MCP refresh failed: %s: %s", type(exc).__name__, exc)
        await asyncio.sleep(interval)


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
        "flash_loan_amount": str(opportunity.flash_loan_amount),
        "flash_multiplier": str(opportunity.flash_multiplier.quantize(Decimal("0.01"))),
        "gross_profit_quote": str(opportunity.gross_profit_quote),
        "net_profit_quote": str(opportunity.net_profit_quote),
        "gas_cost_quote": str(opportunity.gas_cost_quote),
        "flash_loan_fee_quote": str(opportunity.flash_loan_fee_quote),
        "safety_buffer_quote": str(opportunity.safety_buffer_quote),
        "status": "ready_for_fresh_simulation",
    }


async def to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)


async def scan_evm_chain(adapter, chain_id, *, max_quote, taker, slippage, min_profit, safety_buffer):
    """Scan every venue pair on one EVM chain for one base token."""
    spec = get_spec(chain_id)
    quote_token, quote_decimals = _quote_token_for(chain_id)
    base_tokens = _base_tokens_for(chain_id)
    venue_names = configured_base_venues(chain_id)
    flash_enabled = env_bool("FLASH_LOAN_ENABLED", True)
    configured_fee_bps = env_decimal("FLASH_LOAN_FEE_BPS", "0")
    fee_bps = configured_fee_bps
    if flash_enabled and hasattr(adapter, "evm") and adapter.evm is not None:
        try:
            live_fee_bps = Decimal(str(adapter.evm.flash_loan_fee_bps(chain_id)))
        except Exception as exc:
            raise RuntimeError(
                f"cannot verify live Aave flash-loan premium on chain {chain_id}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if live_fee_bps < 0 or live_fee_bps > Decimal("1000"):
            raise ValueError(f"invalid live flash-loan fee on chain {chain_id}: {live_fee_bps} bps")
        fee_bps = live_fee_bps
    engine = DexCrossExchangeEngine(
        adapter, venue_names, min_profit=min_profit, quote_token_decimals=quote_decimals,
        safety_buffer_quote=safety_buffer,
        flash_loan_enabled=flash_enabled,
        flash_loan_fee_bps=fee_bps,
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
    admin_server = None
    admin_state = {"enabled": False}
    try:
        admin_server, admin_state = start_admin_server()
    except Exception as exc:
        # Diagnostics must never prevent the trading scanner from starting.
        logging.exception("Dragon gRPC admin interface failed to start: %s", exc)
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
        if min_profit < Decimal("0.002"):
            raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.002")
        safety = Decimal("0")
        slippage = int(os.getenv("DEX_SLIPPAGE_BPS", "50"))
        flash_cap_quote = env_decimal("DEX_FLASH_LOAN_LIQUIDITY_QUOTE", "10000")
        if flash_cap_quote <= 0:
            raise ValueError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE must be positive")
        taker = os.getenv("DEX_TAKER_ADDRESS", "").strip() or "0x000000000000000000000000000000000000dEaD"
        poll = max(1.0, float(os.getenv("DEX_POLL_SECONDS", "30")))
        min_profit_floor = env_decimal("DEX_MIN_NET_PROFIT_FLOOR", "0.002")
        if min_profit_floor < Decimal("0.002"):
            raise ValueError("DEX_MIN_NET_PROFIT_FLOOR cannot be below 0.002")
        dynamic_profit = env_bool("DEX_DYNAMIC_MIN_PROFIT", True)
        logging.info("Dragon opportunity scan cycle configured at %.1fs dynamic_profit=%s floor=%s no_ceiling=true", poll, dynamic_profit, min_profit_floor)
        triangular_enabled = env_bool("TRIANGULAR_ARBITRAGE_ENABLED", True)
        capacity = ExecutionCapacity(initial=int(os.getenv("DEX_MAX_EXECUTION_CONCURRENCY", "8")), maximum=None)
        sponsor_manager = GasSponsorManager(min_net_profit=min_profit)
        economic_agent = EconomicDecisionAgent(history_size=int(os.getenv("ECONOMIC_AGENT_HISTORY_SIZE", "256")), min_profit=min_profit)
        dragon_core = DragonCore(min_profit=min_profit)
        proof_engine = BaseProofEngine(min_profit=min_profit)
        atomic_simulator = BaseAtomicSimulator(min_profit=min_profit)
        five_circle = FiveCircleEngine(
            min_profit=min_profit,
            gas_stress_bps=env_decimal("DRAGON_CHALLENGE_GAS_BPS", "2000"),
            execution_stress_bps=env_decimal("DRAGON_CHALLENGE_EXECUTION_BPS", "100"),
            max_candidates=int(os.getenv("DRAGON_CHALLENGE_MAX_CANDIDATES", "8")),
        )
        # Three top-level brain engines. Specialized brains remain underneath these boundaries.
        base_discovery_venues = configured_base_venues(8453)
        discovery_brain = DexCrossExchangeEngine(
            adapter,
            base_discovery_venues,
            min_profit=min_profit,
            quote_token_decimals=6,
            flash_loan_enabled=env_bool("FLASH_LOAN_ENABLED", True),
            flash_loan_fee_bps=env_decimal("FLASH_LOAN_FEE_BPS", "0"),
            telemetry=METRICS,
        )
        brain_engines = ThreeBrainEngines(
            discovery=discovery_brain,
            economics=economic_agent,
            execution=atomic_simulator,
            min_profit=min_profit,
        )
        brain_engines.last_stage = "ready"
        base_reader = BaseLiveReader()
        rotation = 0

        stable_vaults = []
        stable_vault_error = None
        if env_bool("AAVE_STABLE_VAULTS_ENABLED", False):
            try:
                stable_vaults = load_validated_stable_vaults()
                logging.info("Aave Stable Vault configuration loaded vaults=%s", len(stable_vaults))
            except Exception as exc:
                stable_vault_error = f"{type(exc).__name__}: {exc}"
                logging.error("Aave Stable Vault configuration invalid: %s", stable_vault_error)

        chain_details = {}
        valid_evm_chains = []
        quote_validation_errors = {}
        for cid in evm_chains:
            spec = get_spec(cid)
            try:
                quote_token, quote_decimals = _validate_quote_token_config(cid)
                venues = configured_base_venues(cid)
                if not venues:
                    raise ValueError(f"no allowed Base DEX venues configured for chain {cid}")
                valid_evm_chains.append(cid)
                chain_details[spec.name] = {
                    "chain_id": cid,
                    "family": "evm",
                    "venues": venues,
                    "quote_token": quote_token,
                    "quote_decimals": quote_decimals,
                    "base_tokens": _base_tokens_for(cid),
                    "flash_scan_cap_quote": str(flash_cap_quote),
                    "flash_scan_cap_raw": str(quote_units(flash_cap_quote, quote_decimals)),
                    "quote_config_valid": True,
                }
                logging.info("quote config valid chain=%s id=%s token=%s decimals=%s", spec.name, cid, quote_token, quote_decimals)
            except Exception as exc:
                quote_validation_errors[spec.name] = str(exc)
                logging.error("quote config invalid chain=%s id=%s: %s", spec.name, cid, exc)
                chain_details[spec.name] = {
                    "chain_id": cid,
                    "family": "evm",
                    "venues": list(adapter.sources(cid)),
                    "quote_config_valid": False,
                    "quote_config_error": str(exc),
                    "status": "disabled",
                }
        evm_chains = valid_evm_chains
        if quote_validation_errors:
            logging.warning("disabled chains due to invalid quote configuration: %s", quote_validation_errors)
        if not evm_chains and not nonevm_chains:
            raise ValueError("no chains passed startup configuration validation")
        for family in nonevm_chains:
            chain_details[family] = {
                "chain_id": family,
                "family": "non-evm",
                "venues": list(adapter.sources(family)),
            }
        for cid in evm_chains:
            try:
                dragon_core.observe_chain(ChainState(chain_id=cid, block_timestamp=time.time()))
            except Exception:
                logging.debug("Dragon Core chain observation failed", exc_info=True)

        with LOCK:
            STATE.update({
                "status": "running",
                "triangular_enabled": triangular_enabled,
                "gas_sponsor_enabled": env_bool("GAS_SPONSOR_ENABLED", True),
                "gas_sponsor_required": env_bool("GAS_SPONSOR_REQUIRED", False),
                "execution_capacity": capacity.current,
                "mode": "live" if env_bool("LIVE_TRADING", False) else "paper",
                "chains": list(chain_details.keys()),
                "chain_details": chain_details,
                "universe": universe_payload(chain_details),
                "sources": sorted({v for d in chain_details.values() for v in d["venues"]}),
                "min_net_profit": str(min_profit),
                "safety_buffer": str(safety),
                "flash_cap": str(flash_cap_quote),
                "flash_cap_unit": "human quote units",
                "flash_loan_enabled": env_bool("FLASH_LOAN_ENABLED", True),
                "compounding_enabled": env_bool("DEX_COMPOUND_PROFITS", False),
                "rpc_providers": provider_status(load_providers()),
                "rpc_hosts": {
                    get_spec(cid).name: [rpc_host(u) for u in rpc_urls_for(cid)]
                    for cid in evm_chains
                },
                "aave_stable_vaults_enabled": env_bool("AAVE_STABLE_VAULTS_ENABLED", False),
                "aave_stable_vaults": stable_vaults,
                "aave_stable_vault_error": stable_vault_error,
                "brain_engines": brain_engines.snapshot(),
            })
        if not any(provider_status(load_providers()).values()):
            logging.warning(
                "No private RPC keys detected (ALCHEMY_API_KEY / INFURA_API_KEY / QUICKNODE_*); "
                "using public endpoints, which are rate-limited."
            )
        total_venues = sum(len(d["venues"]) for d in chain_details.values())
        logging.info("Dragon multi-chain ready chains=%s venues=%s min_profit=%s", list(chain_details.keys()), total_venues, min_profit)

        aave_task = asyncio.create_task(refresh_aave_mcp())
        aave_user_state_task = asyncio.create_task(refresh_aave_user_state())
        aave_horizon_task = asyncio.create_task(refresh_aave_horizon())
        aave_supply_yield_task = asyncio.create_task(refresh_aave_supply_yield())
        aave_umbrella_task = asyncio.create_task(refresh_aave_umbrella())
        aave_v4_task = asyncio.create_task(refresh_aave_v4())
        aave_v4_liquidity_task = asyncio.create_task(refresh_aave_v4_liquidity())

        while True:
            try:
                rotation += 1
                if dynamic_profit:
                    cycle_profit_floor = env_decimal("DEX_MIN_NET_PROFIT_FLOOR", str(min_profit_floor))
                    current_env_profit = env_decimal("DEX_MIN_NET_PROFIT", str(min_profit))
                    min_profit = max(cycle_profit_floor, current_env_profit)
                    sponsor_manager.min_net_profit = min_profit
                    economic_agent.min_profit = min_profit
                    dragon_core.min_profit = min_profit
                    proof_engine.min_profit = min_profit
                    atomic_simulator.min_profit = min_profit
                    five_circle.min_profit = min_profit
                    brain_engines.min_profit = min_profit
                    with LOCK:
                        STATE["dynamic_min_net_profit"] = str(min_profit)
                        STATE["dynamic_min_net_profit_floor"] = str(cycle_profit_floor)
                    logging.info("Dragon cycle=%s dynamic min_net_profit=%s floor=%s no_ceiling=true", rotation, min_profit, cycle_profit_floor)
                all_found = []
                try:
                    base_snapshot = await to_thread(base_reader.snapshot)
                    base_chain = ChainState(chain_id=8453, block_number=base_snapshot["block_number"], block_timestamp=base_snapshot["block_timestamp"], gas_quote=Decimal(str(base_snapshot.get("base_fee_wei", "0"))), rpc_latency_ms=Decimal(str(base_snapshot["rpc_latency_ms"])))
                    dragon_core.observe_chain(base_chain)
                    with LOCK:
                        STATE["base_live"] = base_snapshot
                except Exception as exc:
                    with LOCK:
                        STATE["base_live"] = {"connected": False, "error": f"{type(exc).__name__}: {exc}"}
                    logging.warning("Base live read failed: %s: %s", exc, exc)
                rejections = {}
                brain_engines.last_stage = "discovery"
                # Phase 1: cheap gross-spread probes across every active chain.
                # Phase 2: spend the expensive full sizing/second-leg scan only
                # on chains whose probe economics clear the flash-fee hurdle.
                chain_workers = max(1, min(len(evm_chains), int(os.getenv("DEX_CHAIN_CONCURRENCY", str(len(evm_chains))))))
                chain_sem = asyncio.Semaphore(chain_workers)
                probe_fraction = max(Decimal("0.01"), min(Decimal("0.20"), env_decimal("DEX_CHAIN_PROBE_FRACTION", "0.05")))
                probe_min_bps = env_decimal("DEX_CHAIN_PROBE_MIN_BPS", "5")

                async def _probe_chain(cid):
                    async with chain_sem:
                        quote_token, quote_decimals = _quote_token_for(cid)
                        base_tokens = _base_tokens_for(cid)
                        cap = quote_units(flash_cap_quote * probe_fraction, quote_decimals)
                        if cap <= 0 or not base_tokens:
                            return cid, Decimal("-Infinity"), {"probe_invalid": 1}
                        # Read the live Aave premium once per chain and use it in
                        # the cheap ranking. A configured fee is only a fallback
                        # when flash loans are disabled.
                        fee_bps = env_decimal("FLASH_LOAN_FEE_BPS", "0")
                        if env_bool("FLASH_LOAN_ENABLED", True) and hasattr(adapter, "evm") and adapter.evm is not None:
                            fee_bps = Decimal(str(adapter.evm.flash_loan_fee_bps(cid)))
                        venue_names = configured_base_venues(cid)
                        engine = DexCrossExchangeEngine(
                            adapter, venue_names, min_profit=min_profit,
                            quote_token_decimals=quote_decimals,
                            flash_loan_enabled=env_bool("FLASH_LOAN_ENABLED", True),
                            flash_loan_fee_bps=fee_bps, telemetry=METRICS,
                        )
                        best = Decimal("-Infinity")
                        for base in base_tokens:
                            probe = await to_thread(
                                engine.fast_probe,
                                chain_id=cid, quote_token=quote_token, base_token=base,
                                quote_amount=cap, taker=taker, slippage_bps=slippage,
                            )
                            if probe > best:
                                best = probe
                        # Probe score is deliberately conservative: estimated
                        # gross spread minus the live flash premium. Gas remains a
                        # hard gate in the full scan and is never assumed away.
                        score = best - fee_bps
                        return cid, score, {"probe_gross_bps": best, "flash_fee_bps": fee_bps}

                probe_results = await asyncio.gather(
                    *(_probe_chain(cid) for cid in evm_chains),
                    return_exceptions=True,
                )
                probe_scores: dict[int, Decimal] = {}
                ranked = []
                for result in probe_results:
                    if isinstance(result, Exception):
                        rejections["chain_probe_error"] = rejections.get("chain_probe_error", 0) + 1
                        logging.warning("chain probe failed: %s: %s", type(result).__name__, result)
                        continue
                    cid, score, stats = result
                    probe_scores[int(cid)] = score
                    if score.is_finite():
                        ranked.append((score, cid, stats))
                    else:
                        rejections["chain_probe_no_signal"] = rejections.get("chain_probe_no_signal", 0) + 1
                ranked.sort(reverse=True)

                # Probe results are for prioritisation/telemetry only. They must
                # never remove a healthy configured chain from the scan. This was
                # the bug that made a large watchlist appear to have only one
                # working chain.
                selected = economic_agent.prioritize_chains(evm_chains, probe_scores)
                if not selected:
                    selected = list(evm_chains)
                logging.info(
                    "full multi-chain scan selected=%s configured=%s",
                    len(selected), len(evm_chains),
                )

                async def _scan_selected_chain(cid):
                    async with chain_sem:
                        quote_token, quote_decimals = _quote_token_for(cid)
                        chain_flash_cap = quote_units(flash_cap_quote, quote_decimals)
                        return cid, await scan_evm_chain(
                            adapter, cid, max_quote=chain_flash_cap, taker=taker,
                            slippage=slippage, min_profit=min_profit, safety_buffer=safety,
                        )

                chain_results = await asyncio.gather(
                    *(_scan_selected_chain(cid) for cid in selected),
                    return_exceptions=True,
                )
                for result in chain_results:
                    if isinstance(result, Exception):
                        rejections["chain_scan_error"] = rejections.get("chain_scan_error", 0) + 1
                        logging.warning("chain scan failed: %s: %s", type(result).__name__, result)
                        continue
                    cid, (found, rej) = result
                    for opp in found:
                        all_found.append((opp, get_spec(cid).name))
                    for k, v in rej.items():
                        rejections[k] = rejections.get(k, 0) + int(v)
                triangular_results = []
                if triangular_enabled:
                    triangular_results = await asyncio.gather(
                        *(scan_triangular_chain(adapter, cid,
                          max_quote=quote_units(flash_cap_quote, _quote_token_for(cid)[1]),
                          min_profit=min_profit) for cid in selected),
                        return_exceptions=True,
                    )
                tri_count = 0
                for result in triangular_results:
                    if isinstance(result, Exception):
                        rejections["triangular_scan_error"] = rejections.get("triangular_scan_error", 0) + 1
                        logging.warning("triangular scan failed: %s: %s", type(result).__name__, result)
                        continue
                    found, rej = result
                    tri_count += len(found)
                    for opp in found:
                        all_found.append((opp, get_spec(opp.route.chain_id).name))
                    for k, v in rej.items():
                        rejections[k] = rejections.get(k, 0) + int(v)
                with LOCK:
                    STATE["triangular_opportunities"] = tri_count
                    STATE["execution_capacity"] = capacity.current
                for family in nonevm_chains:
                    _, rej = await scan_nonevm_chain(adapter, family, slippage=slippage)
                    for k, v in rej.items():
                        rejections[k] = rejections.get(k, 0) + int(v)
                merge_rejections(rejections)
                brain_engines.last_stage = "economics"
                # Five-circle gate: fast discovery/optimization, adversarial stress,
                # explicit proof status, then an execution gate. The current runtime
                # remains paper-only until an exact simulator result is supplied.
                base_block = int(STATE.get("base_live", {}).get("block_number", 0))
                base_candidates = [opp for opp, _label in all_found if getattr(opp, "chain_id", None) == 8453]
                proof_passed = False
                atomic_result = None
                proof_candidate = max(base_candidates, key=lambda x: Decimal(str(getattr(x, "net_profit_quote", "-Infinity"))), default=None)
                if proof_candidate is not None:
                    try:
                        proof = await to_thread(
                            proof_engine.prove,
                            adapter=adapter,
                            opportunity=proof_candidate,
                            taker=taker,
                            slippage_bps=slippage,
                        )
                        proof_passed = bool(proof.passed)
                        if proof_passed:
                            atomic_result = await to_thread(
                                atomic_simulator.simulate,
                                adapter=adapter,
                                opportunity=proof_candidate,
                                taker=taker,
                                slippage_bps=slippage,
                            )
                            proof_passed = bool(atomic_result.passed)
                    except Exception:
                        logging.exception("Base proof gate failed")
                brain_engines.last_stage = "execution"
                circle_result = five_circle.run(
                    base_candidates,
                    rotation=rotation,
                    chain_id=8453,
                    block_number=base_block,
                    simulation_passed=proof_passed,
                )
                with LOCK:
                    STATE["brain_engines"] = brain_engines.snapshot()
                    STATE["five_circle"] = {
                        "rotation": rotation,
                        "status": "atomic_simulation_passed" if atomic_result is not None and atomic_result.passed else ("ready_for_atomic_simulation" if proof_candidate is not None else "rejected"),
                        "atomic_simulation": None if atomic_result is None else {"passed": atomic_result.passed, "block_number": atomic_result.block_number, "gas_estimate": atomic_result.gas_estimate, "reason": atomic_result.reason, "target": atomic_result.target},
                        "selected": None if circle_result.selected is None else {
                            "buy_source": getattr(circle_result.selected, "buy_source", None),
                            "sell_source": getattr(circle_result.selected, "sell_source", None),
                            "net_profit_quote": str(getattr(circle_result.selected, "net_profit_quote", "0")),
                        },
                        "decisions": [{"circle": d.circle, "passed": d.passed, "candidate_count": len(d.candidates), "reason": d.reason} for d in circle_result.decisions],
                    }
                opp_chain_labels = {id(opp): chain_label for opp, chain_label in all_found}
                economic_orders = economic_agent.rank([opp for opp, _ in all_found])
                if economic_orders:
                    all_found = [(order.opportunity, opp_chain_labels.get(id(order.opportunity), get_spec(order.chain_id).name)) for order in economic_orders]
                else:
                    all_found.sort(key=lambda pair: pair[0].net_profit_quote, reverse=True)
                top = all_found
                with LOCK:
                    STATE["economic_agent"] = {
                        "enabled": True,
                        "last_update": time.time(),
                        "ranked_orders": [{"chain_id": order.chain_id, "net_profit_quote": str(order.net_profit_quote), "expected_net_profit_quote": str(order.expected_net_profit_quote), "execution_probability": str(order.execution_probability), "freshness_factor": str(order.freshness_factor), "latency_factor": str(order.latency_factor), "capital_efficiency": str(order.capital_efficiency), "economic_priority": str(order.economic_priority), "reason": order.reason} for order in economic_orders],
                        "chain_memory": economic_agent.snapshot(),
                    }
                # Feed normalized opportunities into the Dragon Core. This is a paper/economic layer only;
                # it never signs or broadcasts a transaction.
                core_candidates = []
                for opp, _label in all_found:
                    if hasattr(opp, "route"):
                        continue
                    try:
                        core_candidates.append(EconomicCandidate(
                            chain_id=int(opp.chain_id),
                            source=f"{opp.buy_source}->{opp.sell_source}",
                            route=(str(opp.buy_source), str(opp.sell_source)),
                            quote_amount=Decimal(str(opp.quote_amount)),
                            gross_profit=Decimal(str(opp.gross_profit_quote)),
                            gas_cost=Decimal(str(opp.gas_cost_quote)),
                            swap_fees=Decimal("0"),
                            borrow_cost=Decimal(str(opp.flash_loan_fee_quote)),
                            slippage_cost=Decimal("0"),
                            mev_cost=Decimal("0"),
                            safety_buffer=Decimal(str(opp.safety_buffer_quote)),
                            freshness_ms=Decimal("0"),
                            latency_ms=Decimal("0"),
                            liquidity_factor=Decimal("1"),
                            execution_probability=Decimal("1"),
                        ))
                    except Exception:
                        logging.debug("Dragon Core opportunity normalization failed", exc_info=True)
                core_orders = dragon_core.rank(core_candidates)
                with LOCK:
                    STATE["dragon_core"] = dragon_core.snapshot()
                economic_order_by_id = {id(order.opportunity): order for order in economic_orders}
                rows = []
                for index, (opp, chain_label) in enumerate(top):
                    if hasattr(opp, "route"):
                        identifier = opportunity_hash(
                            chain_id=opp.route.chain_id,
                            buy_source=opp.route.venues[0],
                            sell_source=opp.route.venues[-1],
                            base_token=opp.route.token_b,
                            quote_token=opp.route.token_a,
                            quote_amount=opp.quote_amount,
                            quote_version="tri-v1",
                        )
                    else:
                        identifier = opportunity_hash(
                            chain_id=opp.chain_id,
                            buy_source=opp.buy_source,
                            sell_source=opp.sell_source,
                            base_token=opp.base_token,
                            quote_token=opp.quote_token,
                            quote_amount=opp.quote_amount,
                            quote_version="v1",
                        )
                    try:
                        METRICS.record_opportunity(opp)
                    except Exception:
                        logging.debug("telemetry record_opportunity failed", exc_info=True)
                    opp_chain_id = opp.route.chain_id if hasattr(opp, "route") else opp.chain_id
                    quote_decimals = int(next((d["quote_decimals"] for d in chain_details.values() if d.get("chain_id") == opp_chain_id), 6))
                    row = triangular_view(opp, identifier, chain_label, quote_decimals) if hasattr(opp, "route") else opportunity_view(opp, identifier, chain_label, quote_decimals)
                    row["hash_algorithm"] = "SHA-256"
                    rows.append(row)
                opportunities = top
                with LOCK:
                    STATE["scans"] += 1
                    STATE["opportunities"] += len(opportunities)
                    STATE["last_scan"] = time.time()
                    STATE["last_error"] = None
                    STATE["opportunity_records"] = rows
                refresh_aerodrome_opportunities()
                brain_engines.last_stage = "verify"
                with LOCK:
                    STATE["brain_engines"] = brain_engines.snapshot()
                logging.info("scan complete chains=%s opportunities=%s rejections=%s aerodrome_candidates=%s", len(evm_chains), len(opportunities), rejections, len(STATE.get("aerodrome", {}).get("opportunities", [])))
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
        if "aave_user_state_task" in locals():
            aave_user_state_task.cancel()
            try:
                await aave_user_state_task
            except asyncio.CancelledError:
                pass
        if "aave_horizon_task" in locals():
            aave_horizon_task.cancel()
            try:
                await aave_horizon_task
            except asyncio.CancelledError:
                pass
        if "aave_umbrella_task" in locals():
            aave_umbrella_task.cancel()
            try:
                await aave_umbrella_task
            except asyncio.CancelledError:
                pass
        if "aave_supply_yield_task" in locals():
            aave_supply_yield_task.cancel()
            try:
                await aave_supply_yield_task
            except asyncio.CancelledError:
                pass
        if "aave_task" in locals():
            aave_task.cancel()
            try:
                await aave_task
            except asyncio.CancelledError:
                pass
        if "aave_v4_task" in locals():
            aave_v4_task.cancel()
            try:
                await aave_v4_task
            except asyncio.CancelledError:
                pass
        if "aave_v4_liquidity_task" in locals():
            aave_v4_liquidity_task.cancel()
            try:
                await aave_v4_liquidity_task
            except asyncio.CancelledError:
                pass
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                logging.exception("failed to close DEX adapter")
        if admin_server is not None:
            try:
                admin_server.stop(0)
            except Exception:
                logging.exception("failed to stop Dragon gRPC admin interface")


if __name__ == "__main__":
    asyncio.run(main())