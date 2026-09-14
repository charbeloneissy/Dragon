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
        if s.get("status") != "TRADING" or s.get("quoteAsset") is None:
            continue
        symbols[(s["baseAsset"], s["quoteAsset"])] = s["symbol"]
    assets = {b for b, q in symbols if q == "USDT"}
    out = []
    for a, b in combinations(sorted(assets), 2):
        pairs = None
        for x, y in ((a, b), (b, a)):
            if (a, b) in symbols and (b, "USDT") in symbols and (a, "USDT") in symbols:
                pairs = (symbols[(a, b)], symbols[(b, "USDT")], symbols[(a, "USDT")]); break
            if (b, a) in symbols and (a, "USDT") in symbols and (b, "USDT") in symbols:
                pairs = (symbols[(b, a)], symbols[(a, "USDT")], symbols[(b, "USDT")]); break
        if pairs:
            out.append(Triangle(pairs, ("USDT", a, b)))
        if len(out) >= max_triangles:
            break
    return out


def _buy(q):
    return Decimal(str(q["askPrice"])), Decimal(str(q["askQty"]))


def _sell(q):
    return Decimal(str(q["bidPrice"])), Decimal(str(q["bidQty"]))


def evaluate_triangle(t: Triangle, books: dict[str, dict], fee_bps: float, slippage_bps: float):
    if not all(s in books for s in t.symbols):
        return None
    # Evaluate both possible orientations using top-of-book executable prices.
    candidates = []
    # Generic simulation based on the three actual symbols. Try permutations of the asset cycle.
    usdt, a, b = t.assets
    pair_ab = next((s for s in t.symbols if {s[:len(s)//2], s[len(s)//2:]}), None)
    # Use symbol metadata-free matching by checking whether a symbol begins/ends with asset names.
    def find(x, y):
        return next((s for s in t.symbols if s == x + y or s == y + x), None)
    ab, bu, au = find(a,b), find(b,"USDT"), find(a,"USDT")
    if not all((ab, bu, au)):
        return None
    def conv(amount, symbol, src, dst):
        q = books[symbol]
        base = next(iter(()), None)
        if symbol == src + dst:
            return amount / Decimal(str(q["askPrice"])), "BUY"
        return amount * Decimal(str(q["bidPrice"])), "SELL"
    # Two cycles: USDT -> A -> B -> USDT and USDT -> B -> A -> USDT.
    for first, second, direct in ((a,b,ab),(b,a,ab)):
        amount = Decimal("1")
        # USDT -> first
        s_us = find(first,"USDT")
        p1 = books[s_us]
        if s_us == first + "USDT": amount /= Decimal(str(p1["askPrice"]))
        else: amount *= Decimal(str(p1["bidPrice"]))
        s_mid = ab
        p2 = books[s_mid]
        if s_mid == first + second: amount *= Decimal(str(p2["bidPrice"]))
        else: amount /= Decimal(str(p2["askPrice"]))
        s_last = find(second,"USDT")
        p3 = books[s_last]
        if s_last == second + "USDT": amount *= Decimal(str(p3["bidPrice"]))
        else: amount /= Decimal(str(p3["askPrice"]))
        gross_bps = (amount - 1) * Decimal("10000")
        net_bps = gross_bps - Decimal(str(3*fee_bps)) - Decimal(str(3*slippage_bps))
        candidates.append((net_bps, gross_bps, (s_us,s_mid,s_last), first, second))
    return max(candidates, key=lambda x: x[0]) if candidates else None
