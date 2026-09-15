import os
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations


@dataclass(frozen=True)
class Triangle:
    symbols: tuple[str, str, str]
    assets: tuple[str, str, str]


def _excluded_assets() -> set[str]:
    raw = os.getenv("EXCLUDED_BASE_ASSETS", "")
    return {asset.strip().upper() for asset in raw.split(",") if asset.strip()}


def build_triangles(exchange_info: dict, max_triangles: int = 5000):
    markets = {}
    for s in exchange_info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        markets[(s["baseAsset"], s["quoteAsset"])] = s["symbol"]

    excluded = _excluded_assets()
    usdt_assets = sorted(base for base, quote in markets if quote == "USDT" and base not in excluded)
    out = []
    seen = set()
    unlimited = max_triangles <= 0
    for a, b in combinations(usdt_assets, 2):
        if a in excluded or b in excluded:
            continue
        a_usdt = markets.get((a, "USDT"))
        b_usdt = markets.get((b, "USDT"))
        if not a_usdt or not b_usdt:
            continue
        for first, second in ((a, b), (b, a)):
            if first in excluded or second in excluded:
                continue
            cross = markets.get((first, second))
            if not cross:
                continue
            tri = Triangle((markets[(first, "USDT")], cross, markets[(second, "USDT")]), ("USDT", first, second))
            if tri.symbols in seen:
                continue
            seen.add(tri.symbols)
            out.append(tri)
            if not unlimited and len(out) >= max_triangles:
                return out
    return out


def _walk(symbol: str, side: str, qty: Decimal, books: dict):
    book = books.get(symbol, {})
    levels = book.get("bids" if side == "sell" else "asks") or []
    remaining = qty
    result = Decimal("0")
    for raw_price, raw_qty in levels:
        price = Decimal(str(raw_price))
        level_qty = Decimal(str(raw_qty))
        if price <= 0 or level_qty <= 0:
            continue
        if side == "sell":
            take = min(remaining, level_qty)
            result += take * price
            remaining -= take
        else:
            take_base = min(level_qty, remaining / price)
            result += take_base
            remaining -= take_base * price
        if remaining <= 0:
            break
    if remaining > 0 or result <= 0:
        return None
    return result


def _dynamic_safety_bps(t: Triangle, books: dict, symbol_meta: dict, start: Decimal, max_safety_bps: float) -> Decimal:
    """Select latency/repricing safety from live top-level liquidity.

    Actual book impact is already priced by _walk. This is only an additional
    safety margin: 3 bps for deep books, 5 bps for normal books, 10 bps for
    thinner books and 20 bps for very thin books, capped by configuration.
    """
    cap = Decimal(str(max(0.0, max_safety_bps)))
    if cap <= 0:
        return Decimal("0")

    amount = start
    ratios = []
    for i, symbol in enumerate(t.symbols):
        meta = symbol_meta.get(symbol)
        if not meta:
            return cap
        src = t.assets[i]
        dst = t.assets[(i + 1) % 3]
        base, quote = meta
        if src == quote and dst == base:
            side = "buy"
        elif src == base and dst == quote:
            side = "sell"
        else:
            return cap
        levels = books.get(symbol, {}).get("bids" if side == "sell" else "asks") or []
        if not levels:
            return cap
        price = Decimal(str(levels[0][0]))
        qty = Decimal(str(levels[0][1]))
        if price <= 0 or qty <= 0:
            return cap
        top_value = qty * price
        ratios.append(amount / top_value if top_value > 0 else Decimal("999"))
        out = _walk(symbol, side, amount, books)
        if out is None:
            return cap
        amount = out

    worst = max(ratios, default=Decimal("999"))
    if worst <= Decimal("0.25"):
        selected = Decimal("3")
    elif worst <= Decimal("0.75"):
        selected = Decimal("5")
    elif worst <= Decimal("1.5"):
        selected = Decimal("10")
    else:
        selected = Decimal("20")
    return min(cap, selected)


def evaluate_triangle(t: Triangle, books, fee_bps, slippage_bps, symbol_meta=None, notional_usdt=1.0):
    """Evaluate the exact three-leg path using live depth and fees."""
    symbol_meta = symbol_meta or {}
    start = Decimal(str(notional_usdt))
    if start <= 0 or not all(s in books for s in t.symbols):
        return None

    amount = start
    used = []
    fee_factor = Decimal("1") - Decimal(str(fee_bps)) / Decimal("10000")
    if fee_factor <= 0:
        return None

    for i, symbol in enumerate(t.symbols):
        meta = symbol_meta.get(symbol)
        if not meta:
            return None
        src = t.assets[i]
        dst = t.assets[(i + 1) % 3]
        base, quote = meta
        if src == quote and dst == base:
            side = "buy"
        elif src == base and dst == quote:
            side = "sell"
        else:
            return None
        amount = _walk(symbol, side, amount, books)
        if amount is None:
            return None
        amount *= fee_factor
        if amount <= 0:
            return None
        used.append(symbol)

    gross_bps = (amount / start - Decimal("1")) * Decimal("10000")
    safety_bps = _dynamic_safety_bps(t, books, symbol_meta, start, slippage_bps)
    net_bps = gross_bps - safety_bps
    return net_bps, gross_bps, tuple(used), t.assets[1], t.assets[2]
