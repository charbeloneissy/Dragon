"""Pure quantitative research layer for Dragon.

No order placement or balance mutation occurs here. The module is suitable for
market-data replay, paper trading, and quantitative diagnostics.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from math import log
from statistics import median, pstdev

D = Decimal
BPS = D("10000")


@dataclass(frozen=True)
class LegMeasurement:
    symbol: str
    side: str
    input_amount: D
    gross_output: D
    net_output: D
    vwap: D
    slippage_bps: D
    fee_quote: D
    impact_bps: D

    @property
    def output_base(self) -> D:
        """Compatibility alias for callers that consume leg output directly."""
        return self.net_output


@dataclass(frozen=True)
class TriangleMeasurement:
    path: tuple[str, ...]
    starting_usdt: D
    final_usdt_before_fees: D
    final_usdt: D
    gross_pnl_usdt: D
    net_pnl_usdt: D
    gross_edge_bps: D
    fee_bps: D
    slippage_bps: D
    impact_bps: D
    legs: tuple[LegMeasurement, ...]


@dataclass(frozen=True)
class SpreadStats:
    count: int
    mean_bps: D
    median_bps: D
    min_bps: D
    max_bps: D
    std_bps: D
    persistence: D


@dataclass(frozen=True)
class VolatilityStats:
    count: int
    vol_1s: D
    vol_5s: D
    vol_15s: D
    vol_60s: D
    vol_300s: D
    vol_900s: D


@dataclass(frozen=True)
class ResearchDecision:
    hard_gates: dict[str, bool]
    net_edge_bps: D
    expected_pnl_usdt: D
    p_success: D
    expected_value_usdt: D
    score: D
    tier: str
    rejection_reasons: tuple[str, ...]


def _book(book: dict, side: str) -> list[tuple[D, D]]:
    key = "asks" if side == "buy" else "bids"
    return [(D(str(p)), D(str(q))) for p, q in (book.get(key) or []) if D(str(p)) > 0 and D(str(q)) > 0]


def _consume(levels: list[tuple[D, D]], quantity: D) -> tuple[D, D]:
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    remaining, value, filled = quantity, D("0"), D("0")
    for price, available in levels:
        take = min(remaining, available)
        value += take * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled < quantity:
        raise ValueError("insufficient order-book depth")
    return value / filled, filled


def measure_leg(symbol: str, side: str, input_amount: D, book: dict, fee_bps: D) -> LegMeasurement:
    if input_amount <= 0:
        raise ValueError("input amount must be positive")
    levels = _book(book, side)
    if not levels:
        raise ValueError("empty order book")
    best = levels[0][0]
    if side == "buy":
        base_qty = input_amount / best
        vwap, _ = _consume(levels, base_qty)
        gross_output = input_amount / vwap
        fee_quote = input_amount * fee_bps / BPS
        net_output = gross_output * (D("1") - fee_bps / BPS)
    elif side == "sell":
        vwap, _ = _consume(levels, input_amount)
        gross_output = input_amount * vwap
        fee_quote = gross_output * fee_bps / BPS
        net_output = gross_output - fee_quote
    else:
        raise ValueError("side must be buy or sell")
    slip = max(D("0"), ((vwap - best) / best if side == "buy" else (best - vwap) / best) * BPS)
    return LegMeasurement(symbol, side, input_amount, gross_output, net_output, vwap, slip, fee_quote, slip)


def measure_triangle(path: tuple[str, ...], symbols: tuple[str, ...], assets: tuple[str, ...], books: dict[str, dict], symbol_meta: dict[str, tuple[str, str]], starting_usdt: D, fee_bps: D) -> TriangleMeasurement:
    if starting_usdt <= 0 or len(symbols) != 3 or len(assets) != 3:
        raise ValueError("triangle inputs are invalid")
    current = starting_usdt
    gross_current = starting_usdt
    legs: list[LegMeasurement] = []
    for i, symbol in enumerate(symbols):
        src, dst = assets[i], assets[(i + 1) % 3]
        base, quote = symbol_meta[symbol]
        if src == quote and dst == base:
            side = "buy"
        elif src == base and dst == quote:
            side = "sell"
        else:
            raise ValueError("triangle leg does not match symbol metadata")
        leg = measure_leg(symbol, side, current, books[symbol], fee_bps)
        legs.append(leg)
        gross_current = gross_current * (leg.gross_output / leg.input_amount)
        current = leg.net_output
    net_pnl = current - starting_usdt
    gross_pnl = gross_current - starting_usdt
    fees = sum((x.fee_quote for x in legs), D("0"))
    slippage = sum((x.slippage_bps for x in legs), D("0"))
    impact = max((x.impact_bps for x in legs), default=D("0"))
    return TriangleMeasurement(path, starting_usdt, gross_current, current, gross_pnl, net_pnl, gross_pnl / starting_usdt * BPS, fees / starting_usdt * BPS, slippage, impact, tuple(legs))


class TimeSeries:
    def __init__(self, maxlen: int = 900):
        self.values: deque[tuple[float, D]] = deque(maxlen=maxlen)

    def add(self, timestamp_s: float, value: D) -> None:
        self.values.append((float(timestamp_s), D(str(value))))

    def window(self, seconds: float) -> list[D]:
        if not self.values:
            return []
        cutoff = self.values[-1][0] - seconds
        return [v for t, v in self.values if t >= cutoff]


def spread_stats(series: TimeSeries, window_seconds: float = 30.0) -> SpreadStats:
    values = series.window(window_seconds)
    if not values:
        return SpreadStats(0, D("0"), D("0"), D("0"), D("0"), D("0"), D("0"))
    avg = sum(values, D("0")) / D(len(values))
    sigma = D(str(pstdev([float(v) for v in values]))) if len(values) > 1 else D("0")
    return SpreadStats(len(values), avg, D(str(median(values))), min(values), max(values), sigma, D(sum(v > 0 for v in values)) / D(len(values)))


def volatility_stats(prices: TimeSeries) -> VolatilityStats:
    def vol(window: float) -> D:
        values = prices.window(window)
        returns = [log(float(values[i] / values[i - 1])) for i in range(1, len(values)) if values[i - 1] > 0 and values[i] > 0]
        return D(str(pstdev(returns))) if len(returns) > 1 else D("0")
    return VolatilityStats(len(prices.values), vol(1), vol(5), vol(15), vol(60), vol(300), vol(900))


def latency_penalty_bps(volatility_1s: D, latency_ms: D, k: D = D("2")) -> D:
    if volatility_1s <= 0 or latency_ms <= 0:
        return D("0")
    return k * volatility_1s * (latency_ms / D("1000")).sqrt() * BPS


def empirical_probability(wins: int, losses: int, prior: D = D("1")) -> D:
    if wins < 0 or losses < 0:
        raise ValueError("outcome counts cannot be negative")
    return (D(wins) + prior) / (D(wins + losses) + D("2") * prior)


def expected_value(p_success: D, profit: D, loss: D) -> D:
    p = min(D("1"), max(D("0"), p_success))
    return p * profit - (D("1") - p) * loss


def opportunity_score(net_edge_bps: D, liquidity_utilization: D, persistence: D, latency_penalty: D, volatility_penalty: D) -> D:
    edge = max(D("0"), min(D("100"), net_edge_bps * D("5")))
    liquidity = max(D("0"), min(D("1"), D("1") - liquidity_utilization))
    persistence = max(D("0"), min(D("1"), persistence))
    risk = max(D("0"), min(D("100"), latency_penalty + volatility_penalty))
    return max(D("0"), min(D("100"), edge * D("0.50") + liquidity * D("100") * D("0.30") + persistence * D("100") * D("0.20") - risk))


def tier(score: D) -> str:
    if score >= 90: return "A+"
    if score >= 80: return "A"
    if score >= 70: return "B"
    if score >= 60: return "C"
    return "REJECT"


@dataclass
class OutcomeTracker:
    outcomes: dict[str, deque[bool]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=500)))

    def record(self, key: str, profitable: bool) -> None:
        self.outcomes[key].append(bool(profitable))

    def probability(self, key: str) -> D:
        values = self.outcomes[key]
        return empirical_probability(sum(values), len(values) - sum(values))

    def count(self, key: str) -> int:
        return len(self.outcomes[key])


def hard_gate_map(*, data_fresh: bool, synchronized: bool, sufficient_depth: bool, net_edge_positive: bool, expected_profit_positive: bool, risk_ok: bool) -> dict[str, bool]:
    return {"fresh_data": data_fresh, "synchronized": synchronized, "sufficient_depth": sufficient_depth, "net_edge_positive": net_edge_positive, "expected_profit_positive": expected_profit_positive, "risk_ok": risk_ok}
