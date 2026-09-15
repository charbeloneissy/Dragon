from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .cost_model import net_opportunity


@dataclass(frozen=True)
class VenueLeg:
    venue: str
    kind: str  # CEX_SPOT or DEX_SPOT
    symbol: str
    fee_bps: Decimal
    slippage_bps: Decimal
    gas_quote: Decimal = Decimal("0")
    network_bps: Decimal = Decimal("0")


@dataclass(frozen=True)
class UniverseOpportunity:
    strategy: str  # SPOT_SPOT, TRIANGLE, CEX_DEX, DEX_CEX, DEX_DEX
    path: tuple[str, ...]
    gross_bps: Decimal
    net_bps: Decimal
    cost_bps: Decimal
    gas_quote: Decimal


def evaluate_costs(strategy: str, path: tuple[str, ...], gross_bps: Decimal, legs: list[VenueLeg], notional_quote: Decimal) -> UniverseOpportunity | None:
    fee = sum((x.fee_bps for x in legs), Decimal("0"))
    slippage = sum((x.slippage_bps for x in legs), Decimal("0"))
    gas = sum((x.gas_quote for x in legs), Decimal("0"))
    network = sum((x.network_bps for x in legs), Decimal("0"))
    result = net_opportunity(gross_bps, fee_bps=fee, slippage_bps=slippage, gas_quote=gas, notional_quote=notional_quote, network_bps=network)
    if result.net_bps <= 0:
        return None
    return UniverseOpportunity(strategy=strategy, path=path, gross_bps=gross_bps, net_bps=result.net_bps, cost_bps=result.costs.total_bps, gas_quote=result.gas_quote)


def all_strategy_types() -> tuple[str, ...]:
    return ("SPOT_SPOT", "TRIANGLE", "CEX_DEX", "DEX_CEX", "DEX_DEX")


@dataclass(frozen=True)
class TriangleUniverseDecision:
    tier: str
    qualified: bool
    execution_ready: bool
    score: Decimal
    liquidity_factor: Decimal
    persistence_factor: Decimal
    rejection: str | None = None


def _top_value(book: dict, side: str) -> Decimal:
    levels = book.get("bids" if side == "sell" else "asks") or []
    if not levels:
        return Decimal("0")
    return Decimal(str(levels[0][0])) * Decimal(str(levels[0][1]))


def _leg_side(symbol: str, src: str, dst: str, symbol_meta: dict[str, tuple[str, str]]) -> str | None:
    meta = symbol_meta.get(symbol)
    if not meta:
        return None
    base, quote = meta
    if src == quote and dst == base:
        return "buy"
    if src == base and dst == quote:
        return "sell"
    return None


def classify_triangle(triangle, books: dict, symbol_meta: dict[str, tuple[str, str]], now_ms: float, trade_notional: Decimal, *, stale_ms: int, max_slippage_bps: Decimal, net_edge_bps: Decimal, min_net_edge_bps: Decimal, expected_profit_usdt: Decimal, min_expected_profit_usdt: Decimal) -> TriangleUniverseDecision:
    if trade_notional <= 0:
        return TriangleUniverseDecision("D", False, False, Decimal("0"), Decimal("0"), Decimal("0"), "NO_CAPITAL")

    liquidity_factors = []
    for i, symbol in enumerate(triangle.symbols):
        book = books.get(symbol)
        if not book:
            return TriangleUniverseDecision("D", False, False, Decimal("0"), Decimal("0"), Decimal("0"), "NO_BOOK")
        age = Decimal(str(now_ms - float(book.get("depth_ts", 0))))
        if age > Decimal(str(stale_ms)):
            return TriangleUniverseDecision("D", False, False, Decimal("0"), Decimal("0"), Decimal("0"), "STALE_BOOK")
        side = _leg_side(symbol, triangle.assets[i], triangle.assets[(i + 1) % 3], symbol_meta)
        if side is None:
            return TriangleUniverseDecision("D", False, False, Decimal("0"), Decimal("0"), Decimal("0"), "INVALID_LEG")
        top_value = _top_value(book, side)
        if top_value <= 0:
            return TriangleUniverseDecision("D", False, False, Decimal("0"), Decimal("0"), Decimal("0"), "NO_DEPTH")
        liquidity_factors.append(min(Decimal("1"), top_value / trade_notional))

    liquidity = min(liquidity_factors, default=Decimal("0"))
    # Neutral persistence until enough independent observations and realized
    # outcomes exist to calibrate a genuine success-probability model.
    persistence = Decimal("1")
    if net_edge_bps < Decimal("0"):
        return TriangleUniverseDecision("D", False, False, Decimal("0"), liquidity, persistence, "NEGATIVE_NET_EDGE")

    if liquidity >= Decimal("0.80") and net_edge_bps >= Decimal("10"):
        tier = "A"
    elif liquidity >= Decimal("0.50") and net_edge_bps >= Decimal("7"):
        tier = "B"
    elif liquidity >= Decimal("0.25"):
        tier = "C"
    else:
        tier = "D"

    qualified = tier in {"A", "B", "C"} and net_edge_bps >= min_net_edge_bps
    execution_ready = qualified and expected_profit_usdt >= min_expected_profit_usdt
    if max_slippage_bps < 0:
        execution_ready = False

    score = max(Decimal("0"), net_edge_bps) * liquidity * persistence
    rejection = None if execution_ready else ("EXPECTED_PROFIT" if qualified else "UNIVERSE_FILTER")
    return TriangleUniverseDecision(tier, qualified, execution_ready, score, liquidity, persistence, rejection)
