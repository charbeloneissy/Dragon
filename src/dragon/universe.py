"""Requested chain and native-asset universe.

This catalog is deliberately broader than the executable DEX registry. A chain
can be present here for discovery/watchlist purposes before Dragon has a
verified venue adapter and token-address map for it. The runner exposes the
catalog in health data so operators can see what is active versus pending.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class UniverseChain:
    key: str
    name: str
    native_symbol: str
    family: str
    chain_id: int | None = None
    runtime_key: str | None = None
    network_status: str = "mainnet"

    @property
    def runtime(self) -> str:
        return self.runtime_key or self.key


# Green checkmarked chains from the supplied QuickNode screenshots. Unchecked
# cards were intentionally not added to the requested universe.
REQUESTED_UNIVERSE: tuple[UniverseChain, ...] = (
    UniverseChain("stellar", "Stellar", "XLM", "stellar"),
    UniverseChain("sui", "Sui", "SUI", "sui"),
    UniverseChain("ton", "TON", "TON", "ton"),
    UniverseChain("tron", "TRON", "TRX", "tron"),
    UniverseChain("tempo", "Tempo", "USD", "evm", 4217),
    UniverseChain("unichain", "Unichain", "ETH", "evm", 130),
    UniverseChain("vana", "Vana", "VANA", "evm", 1480),
    UniverseChain("worldchain", "World Chain", "ETH", "evm", 480),
    UniverseChain("xlayer", "X Layer", "OKB", "evm", 196),
    UniverseChain("xrpl", "XRP Ledger", "XRP", "xrpl"),
    UniverseChain("xrpl_evm", "XRP EVM", "XRP", "evm", 1440000),
    UniverseChain("zcash", "Zcash", "ZEC", "zcash"),
    UniverseChain("monad", "Monad", "MON", "evm", 143),
    UniverseChain("morph", "Morph", "ETH", "evm", 2818),
    UniverseChain("near", "NEAR", "NEAR", "near"),
    UniverseChain("optimism", "Optimistic Ethereum", "ETH", "evm", 10),
    UniverseChain("osmosis", "Osmosis", "OSMO", "cosmos", runtime_key="cosmos"),
    UniverseChain("peaq", "Peaq", "PEAQ", "evm", 3338),
    UniverseChain("polkadot", "Polkadot", "DOT", "polkadot"),
    UniverseChain("polygon", "Polygon", "POL", "evm", 137),
    UniverseChain("sahara", "Sahara", "SAHARA", "evm", 313313, network_status="testnet_only"),
    UniverseChain("scroll", "Scroll", "ETH", "evm", 534352),
    UniverseChain("sei", "Sei", "SEI", "evm", 1329),
    UniverseChain("injective", "Injective", "INJ", "evm", 1776),
    UniverseChain("ink", "Ink", "ETH", "evm", 57073),
    UniverseChain("joc", "Japan Open Chain", "JOC", "evm", 81),
    UniverseChain("kaia", "Kaia", "KAIA", "evm", 8217),
    UniverseChain("katana", "Katana", "ETH", "evm", 747474),
    UniverseChain("linea", "Linea", "ETH", "evm", 59144),
    UniverseChain("lisk", "Lisk", "ETH", "evm", 1135),
    UniverseChain("litecoin", "Litecoin", "LTC", "litecoin"),
    UniverseChain("mantle", "Mantle", "MNT", "evm", 5000),
    UniverseChain("megaeth", "MegaETH", "ETH", "evm", 4326),
    UniverseChain("mode", "Mode", "ETH", "evm", 34443),
    UniverseChain("hedera", "Hedera", "HBAR", "evm", 295),
    UniverseChain("hemi", "Hemi", "ETH", "evm", 43111),
    UniverseChain("ault", "Ault", "AULT", "evm", 904),
    UniverseChain("bitcoincash", "Bitcoin Cash", "BCH", "bitcoincash"),
    UniverseChain("solana", "Solana", "SOL", "solana", runtime_key="solana"),
    UniverseChain("ethereum", "Ethereum", "ETH", "evm", 1),
    UniverseChain("base", "Base", "ETH", "evm", 8453),
    UniverseChain("bnb", "BNB Smart Chain", "BNB", "evm", 56),
    UniverseChain("hyperliquid", "Hyperliquid", "HYPE", "evm", 999),
    UniverseChain("arc", "Arc", "USDC", "evm", 5042),
    UniverseChain("arbitrum", "Arbitrum", "ETH", "evm", 42161),
    UniverseChain("robinhood", "Robinhood Chain", "ETH", "evm", 4663),
    UniverseChain("0g", "0G", "0G", "evm", 16661),
    UniverseChain("abstract", "Abstract", "ETH", "evm", 2741),
)


def _requested_keys() -> set[str] | None:
    raw = os.getenv("UNIVERSE_CHAINS", "").strip()
    if not raw:
        return None
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def requested_universe() -> tuple[UniverseChain, ...]:
    selected = _requested_keys()
    if selected is None:
        return REQUESTED_UNIVERSE
    return tuple(item for item in REQUESTED_UNIVERSE if item.key in selected or item.name.lower() in selected)


def universe_payload(chain_details: dict[str, dict]) -> list[dict]:
    """Return safe health/dashboard rows with runtime readiness attached."""
    rows = []
    for item in requested_universe():
        details = chain_details.get(item.runtime, {})
        venues = list(details.get("venues", ()))
        row = asdict(item)
        # EVM arbitrage requires two venues; non-EVM adapters may be single-source
        # quote probes, so their readiness remains venue-presence based.
        row["scan_ready"] = bool(venues) and (item.family != "evm" or len(venues) >= 2)
        row["venues"] = venues
        row["status"] = "active" if row["scan_ready"] else "watchlist"
        rows.append(row)
    return rows
