from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations


@dataclass(frozen=True)
class Triangle:
    symbols: tuple[str, str, str]
    assets: tuple[str, str, str]


def build_triangles(exchange_info: dict, max_triangles: int = 5000):
    symbols = {}
    for s in exchange_info.get("symbols", []):
        if s.get("status") != "TRADING" or not s.get("quoteAsset"):
            continue
        symbols[(s["baseAsset"], s["quoteAsset"])] = s["symbol"]

    assets = {base for base, quote in symbols if quote == "USDT"}
    out = []
    for a, b in combinations(sorted(assets), 2):
        if (a, b) in symbols and (a, "USDT") in symbols and (b, "USDT") in symbols:
            out.append(Triangle((symbols[(a, "USDT")], symbols[(a, b)], symbols[(b, "USDT")]), ("USDT", a, b)))
        elif (b, a) in symbols and (a, "USDT") in symbols and (b, "USDT") in symbols:
            out.append(Triangle((symbols[(b, "USDT")], symbols[(b, a)], symbols[(a, "USDT")]), ("USDT", b, a)))
        if len(out) >= max_triangles:
            break
    return out


def _levels(book: dict, side: str):
    key = "asks" if side == "buy" else "bids"
    return book.get(key) or []


def _convert(asset_from: str, asset_to: str, qty: Decimal, books: dict[str, dict], symbol_meta: dict[str, tuple[str, str]]):
    if asset_from == asset_to:
        return qty

    direct = asset_from + asset_to
    inverse = asset_to + asset_from

    for symbol, side, divide in ((direct, "sell", False), (inverse, "buy", True)):
        book = books.get(symbol)
        if not book or symbol not in symbol_meta:
            continue
        levels = _levels(book, side)
        if not levels:
            continue
        price = Decimal(str(levels[0][0]))
        if price <= 0:
            continue
        return qty / price if divide else qty * price
    return None


def evaluate_triangle(t: Triangle, books: dict[str, dict], fee_bps: float, slippage_bps: float, symbol_meta=None, notional_usdt: float = 1.0):
    """Evaluate USDT -> asset A -> asset B -> USDT using live top-of-book depth.

    This follows the optimized 3582606 strategy: direct/inverse conversion,
    monotonic-staleness handled by the caller, and three-leg fee accounting.
    """
    symbol_meta = symbol_meta or {}
    if not all(s in books for s in t.symbols):
        return None

    start = Decimal(str(notional_usdt))
    usdt, first, second = t.assets
    amount = _convert(usdt, first, start, books, symbol_meta)
    if amount is None:
        return None
    amount = _convert(first, second, amount, books, symbol_meta)
    if amount is None:
        return None
    amount = _convert(second, usdt, amount, books, symbol_meta)
    if amount is None:
        return None

    gross_bps = (amount / start - Decimal("1")) * Decimal("10000")
    net_bps = gross_bps - Decimal(str(3 * fee_bps)) - Decimal(str(3 * slippage_bps))
    return net_bps, gross_bps, t.symbols, first, second
