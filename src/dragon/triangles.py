from dataclasses import dataclass
from decimal import Decimal
from itertools import combinations


@dataclass(frozen=True)
class Triangle:
    symbols: tuple[str, str, str]
    assets: tuple[str, str, str]


def build_triangles(exchange_info: dict, max_triangles: int = 3000):
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


def _find_symbol(t: Triangle, base: str, quote: str):
    for symbol in t.symbols:
        if symbol == base + quote or symbol == quote + base:
            return symbol
    return None


def evaluate_triangle(t: Triangle, books: dict[str, dict], fee_bps: float, slippage_bps: float):
    if not all(s in books for s in t.symbols):
        return None
    _, first, second = t.assets
    s_us = _find_symbol(t, first, "USDT")
    s_mid = _find_symbol(t, first, second)
    s_last = _find_symbol(t, second, "USDT")
    if not all((s_us, s_mid, s_last)):
        return None
    amount = Decimal("1")
    q = books[s_us]
    amount = amount / Decimal(str(q["askPrice"])) if s_us == first + "USDT" else amount * Decimal(str(q["bidPrice"]))
    q = books[s_mid]
    amount = amount * Decimal(str(q["bidPrice"])) if s_mid == first + second else amount / Decimal(str(q["askPrice"]))
    q = books[s_last]
    amount = amount * Decimal(str(q["bidPrice"])) if s_last == second + "USDT" else amount / Decimal(str(q["askPrice"]))
    gross_bps = (amount - 1) * Decimal("10000")
    net_bps = gross_bps - Decimal(str(3 * fee_bps)) - Decimal(str(3 * slippage_bps))
    return net_bps, gross_bps, (s_us, s_mid, s_last), first, second
