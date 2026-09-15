from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from math import log, sqrt

D = Decimal
BPS = D("10000")

DEPTH_BPS = (D("1"), D("2"), D("5"), D("10"), D("25"), D("50"), D("100"))


@dataclass(frozen=True)
class BookMetrics:
    best_bid: D
    best_ask: D
    mid: D
    spread_bps: D
    buy_vwap: D
    sell_vwap: D
    buy_slippage_bps: D
    sell_slippage_bps: D
    total_slippage_bps: D
    imbalance_1: D
    imbalance_5: D
    imbalance_10: D
    imbalance_20: D
    imbalance_50: D
    depth_quote: dict[str, D]
    executable_depth_quote: D
    liquidity_utilization: D
    market_impact_bps: D


def _levels(book: dict, side: str):
    return [(D(str(p)), D(str(q))) for p, q in (book.get("bids" if side == "bid" else "asks") or []) if D(str(p)) > 0 and D(str(q)) > 0]


def _vwap(levels, quantity: D) -> tuple[D, D]:
    if quantity <= 0 or not levels:
        return D("0"), D("0")
    remaining = quantity
    value = D("0")
    filled = D("0")
    for price, qty in levels:
        take = min(remaining, qty)
        value += take * price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled <= 0:
        return D("0"), D("0")
    return value / filled, filled


def _quote_depth(levels, reference: D, distance_bps: D, side: str) -> D:
    if reference <= 0:
        return D("0")
    if side == "bid":
        floor = reference * (D("1") - distance_bps / BPS)
        return sum((p * q for p, q in levels if p >= floor), D("0"))
    ceiling = reference * (D("1") + distance_bps / BPS)
    return sum((p * q for p, q in levels if p <= ceiling), D("0"))


def _imbalance(levels_bid, levels_ask, n: int) -> D:
    bv = sum((p * q for p, q in levels_bid[:n]), D("0"))
    av = sum((p * q for p, q in levels_ask[:n]), D("0"))
    total = bv + av
    return (bv - av) / total if total > 0 else D("0")


def measure_book(book: dict, trade_notional: D) -> BookMetrics:
    bids = _levels(book, "bid")
    asks = _levels(book, "ask")
    if not bids or not asks:
        raise ValueError("order book is empty")
    best_bid, best_ask = bids[0][0], asks[0][0]
    mid = (best_bid + best_ask) / D("2")
    spread_bps = (best_ask - best_bid) / mid * BPS if mid > 0 else D("Infinity")

    buy_qty = trade_notional / best_ask if best_ask > 0 else D("0")
    sell_qty = trade_notional / best_bid if best_bid > 0 else D("0")
    buy_vwap, buy_filled = _vwap(asks, buy_qty)
    sell_vwap, sell_filled = _vwap(bids, sell_qty)
    if buy_vwap <= 0 or sell_vwap <= 0 or buy_filled < buy_qty or sell_filled < sell_qty:
        raise ValueError("insufficient executable depth")

    buy_slip = max(D("0"), (buy_vwap - best_ask) / best_ask * BPS)
    sell_slip = max(D("0"), (best_bid - sell_vwap) / best_bid * BPS)
    depth_quote = {str(int(x)): _quote_depth(bids, best_bid, x, "bid") + _quote_depth(asks, best_ask, x, "ask") for x in DEPTH_BPS}
    executable_depth_quote = min(_quote_depth(bids, best_bid, D("10"), "bid"), _quote_depth(asks, best_ask, D("10"), "ask"))
    utilization = trade_notional / executable_depth_quote if executable_depth_quote > 0 else D("Infinity")
    impact = max(buy_slip, sell_slip)
    return BookMetrics(
        best_bid, best_ask, mid, spread_bps, buy_vwap, sell_vwap,
        buy_slip, sell_slip, buy_slip + sell_slip,
        _imbalance(bids, asks, 1), _imbalance(bids, asks, 5),
        _imbalance(bids, asks, 10), _imbalance(bids, asks, 20), _imbalance(bids, asks, 50),
        depth_quote, executable_depth_quote, utilization, impact,
    )


_HISTORY: dict[str, deque[tuple[float, D]]] = defaultdict(lambda: deque(maxlen=120))


def observe_spread(key: str, timestamp_s: float, spread_bps: D) -> dict[str, D | int]:
    h = _HISTORY[key]
    h.append((timestamp_s, spread_bps))
    values = [v for _, v in h]
    n = len(values)
    mean = sum(values, D("0")) / n if n else D("0")
    ordered = sorted(values)
    median = ordered[n // 2] if n else D("0")
    variance = sum(((v - mean) ** 2 for v in values), D("0")) / n if n else D("0")
    sigma = variance.sqrt() if variance > 0 else D("0")
    minimum = min(values) if values else D("0")
    maximum = max(values) if values else D("0")
    persistence = D("0")
    if n > 1:
        direction = D("1") if values[-1] >= mean else D("-1")
        persistence = D(sum(1 for v in values if (v - mean) * direction >= 0)) / D(n)
    return {"count": n, "mean_bps": mean, "median_bps": median, "min_bps": minimum, "max_bps": maximum, "std_bps": sigma, "persistence": persistence}


def latency_penalty_bps(volatility_1s: D, latency_ms: D, confidence_k: D = D("2")) -> D:
    if volatility_1s <= 0 or latency_ms <= 0:
        return D("0")
    return confidence_k * volatility_1s * (latency_ms / D("1000")).sqrt() * BPS


def success_probability(wins: int, losses: int, prior: D = D("1")) -> D:
    total = wins + losses
    if total <= 0:
        return D("0.5")
    return (D(wins) + prior) / (D(total) + D("2") * prior)


def expected_value(p_success: D, profit: D, loss: D) -> D:
    p = min(D("1"), max(D("0"), p_success))
    return p * profit - (D("1") - p) * loss


def normalized_score(net_bps: D, liquidity_factor: D, persistence: D, latency_penalty: D, volatility_penalty: D) -> D:
    edge = max(D("0"), min(D("100"), net_bps * D("5")))
    liq = max(D("0"), min(D("1"), liquidity_factor))
    per = max(D("0"), min(D("1"), persistence))
    risk = max(D("0"), min(D("100"), latency_penalty + volatility_penalty))
    return max(D("0"), min(D("100"), edge * D("0.50") + liq * D("100") * D("0.30") + per * D("100") * D("0.20") - risk))


def rank_tier(score: D) -> str:
    if score >= D("90"):
        return "A+"
    if score >= D("80"):
        return "A"
    if score >= D("70"):
        return "B"
    if score >= D("60"):
        return "C"
    return "REJECT"
