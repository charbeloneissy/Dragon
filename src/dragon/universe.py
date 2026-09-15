from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .cost_model import net_opportunity

D = Decimal


@dataclass(frozen=True)
class VenueLeg:
    venue: str
    kind: str  # CEX_SPOT or DEX_SPOT
    symbol: str
    fee_bps: Decimal
    slippage_bps: Decimal
    gas_quote: Decimal = D("0")
    network_bps: Decimal = D("0")


@dataclass(frozen=True)
class UniverseOpportunity:
    strategy: str  # SPOT_SPOT, TRIANGLE, CEX_DEX, DEX_CEX, DEX_DEX
    path: tuple[str, ...]
    gross_bps: Decimal
    net_bps: Decimal
    cost_bps: Decimal
    gas_quote: Decimal


def evaluate_costs(strategy: str, path: tuple[str, ...], gross_bps: Decimal, legs: list[VenueLeg], notional_quote: Decimal) -> UniverseOpportunity | None:
    fee = sum((x.fee_bps for x in legs), D("0"))
    slippage = sum((x.slippage_bps for x in legs), D("0"))
    gas = sum((x.gas_quote for x in legs), D("0"))
    network = sum((x.network_bps for x in legs), D("0"))
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
        return D("0")
    return D(str(levels[0][0])) * D(str(levels[0][1]))


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


def extreme_golden_score(*, net_edge_bps: Decimal, liquidity_factor: Decimal, persistence_factor: Decimal) -> Decimal:
    """Dragon's extreme-golden ranking formula.

    Ranking is deliberately separate from the hard execution gates.
    Edge is normalized against a 20 bps reference, then weighted 50%.
    Liquidity and persistence contribute 30% and 20%.  No score can override
    stale data, missing depth, invalid legs, negative edge, or capital gates.
    """
    edge_quality = max(D("0"), min(D("1"), net_edge_bps / D("20")))
    liquidity = max(D("0"), min(D("1"), liquidity_factor))
    persistence = max(D("0"), min(D("1"), persistence_factor))
    score = D("100") * (
        D("0.50") * edge_quality
        + D("0.30") * liquidity
        + D("0.20") * persistence
    )
    return max(D("0"), min(D("100"), score))


def classify_triangle(triangle, books: dict, symbol_meta: dict[str, tuple[str, str]], now_ms: float, trade_notional: Decimal, *, stale_ms: int, max_slippage_bps: Decimal, net_edge_bps: Decimal, min_net_edge_bps: Decimal, expected_profit_usdt: Decimal, min_expected_profit_usdt: Decimal) -> TriangleUniverseDecision:
    if trade_notional <= 0:
        return TriangleUniverseDecision("D", False, False, D("0"), D("0"), D("0"), "NO_CAPITAL")

    liquidity_factors = []
    for i, symbol in enumerate(triangle.symbols):
        book = books.get(symbol)
        if not book:
            return TriangleUniverseDecision("D", False, False, D("0"), D("0"), D("0"), "NO_BOOK")
        age = D(str(now_ms - float(book.get("depth_ts", 0))))
        if age > D(str(stale_ms)):
            return TriangleUniverseDecision("D", False, False, D("0"), D("0"), D("0"), "STALE_BOOK")
        side = _leg_side(symbol, triangle.assets[i], triangle.assets[(i + 1) % 3], symbol_meta)
        if side is None:
            return TriangleUniverseDecision("D", False, False, D("0"), D("0"), D("0"), "INVALID_LEG")
        top_value = _top_value(book, side)
        if top_value <= 0:
            return TriangleUniverseDecision("D", False, False, D("0"), D("0"), D("0"), "NO_DEPTH")
        liquidity_factors.append(min(D("1"), top_value / trade_notional))

    liquidity = min(liquidity_factors, default=D("0"))
    # Persistence remains neutral until independently observed outcomes are
    # available. It must not be fabricated from the same quote snapshot.
    persistence = D("1")

    if net_edge_bps <= D("0"):
        return TriangleUniverseDecision("D", False, False, D("0"), liquidity, persistence, "NEGATIVE_NET_EDGE")
    if net_edge_bps < min_net_edge_bps:
        return TriangleUniverseDecision("D", False, False, extreme_golden_score(net_edge_bps=net_edge_bps, liquidity_factor=liquidity, persistence_factor=persistence), liquidity, persistence, "NET_EDGE")
    if liquidity < D("0.25"):
        return TriangleUniverseDecision("D", False, False, extreme_golden_score(net_edge_bps=net_edge_bps, liquidity_factor=liquidity, persistence_factor=persistence), liquidity, persistence, "INSUFFICIENT_LIQUIDITY")
    if expected_profit_usdt < min_expected_profit_usdt:
        return TriangleUniverseDecision("C", True, False, extreme_golden_score(net_edge_bps=net_edge_bps, liquidity_factor=liquidity, persistence_factor=persistence), liquidity, persistence, "EXPECTED_PROFIT")
    if max_slippage_bps < D("0"):
        return TriangleUniverseDecision("D", False, False, D("0"), liquidity, persistence, "INVALID_SLIPPAGE_LIMIT")

    score = extreme_golden_score(net_edge_bps=net_edge_bps, liquidity_factor=liquidity, persistence_factor=persistence)
    if score >= D("85") and liquidity >= D("0.80"):
        tier = "A+"
    elif score >= D("70") and liquidity >= D("0.50"):
        tier = "A"
    elif score >= D("55") and liquidity >= D("0.25"):
        tier = "B"
    else:
        tier = "C"

    qualified = tier in {"A+", "A", "B", "C"}
    execution_ready = qualified and expected_profit_usdt >= min_expected_profit_usdt
    return TriangleUniverseDecision(tier, qualified, execution_ready, score, liquidity, persistence, None if execution_ready else "EXECUTION_GATE")
