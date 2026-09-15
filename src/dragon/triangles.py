import os
from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations


@dataclass(frozen=True)
class Triangle:
    symbols: tuple[str, str, str]
    assets: tuple[str, str, str]


def _excluded_assets() -> set[str]:
    """Return optional base assets excluded from the triangle universe.

    Configure with a comma-separated environment variable, for example:
    EXCLUDED_BASE_ASSETS=BTC,ETH
    An empty value keeps the full universe unchanged.
    """
    raw = os.getenv("EXCLUDED_BASE_ASSETS", "")
    return {asset.strip().upper() for asset in raw.split(",") if asset.strip()}


def build_triangles(exchange_info: dict, max_triangles: int = 5000):
    """Build every available USDT triangle direction.

    max_triangles <= 0 means no application-level cap. Binance/exchange
    stream limits are handled by the transport layer rather than silently
    deleting candidates from the opportunity graph.

    EXCLUDED_BASE_ASSETS optionally removes triangles containing any listed
    base asset, without removing those assets from Binance market data feeds.
    """
    markets = {}
    for s in exchange_info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        markets[(s["baseAsset"], s["quoteAsset"])] = s["symbol"]

    excluded = _excluded_assets()
    usdt_assets = sorted(
        base for base, quote in markets
        if quote == "USDT" and base not in excluded
    )
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
            tri = Triangle(
                (markets[(first, "USDT")], cross, markets[(second, "USDT")]),
                ("USDT", first, second),
            )
            if tri.symbols in seen:
                continue
            seen.add(tri.symbols)
            out.append(tri)
            if not unlimited and len(out) >= max_triangles:
                return out
    return out


def _walk(symbol: str, side: str, qty: Decimal, books: dict):
    """Consume current order-book depth and return executable output."""
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


def evaluate_triangle(t: Triangle, books, fee_bps, slippage_bps, symbol_meta=None, notional_usdt=1.0):
    """Evaluate the exact configured three-leg path using live depth.

    Depth consumption already captures book impact. The supplied slippage value
    is therefore used once as an execution-safety buffer rather than multiplied
    by three.
    """
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
    safety_bps = Decimal(str(max(0.0, slippage_bps)))
    net_bps = gross_bps - safety_bps
    return net_bps, gross_bps, tuple(used), t.assets[1], t.assets[2]
