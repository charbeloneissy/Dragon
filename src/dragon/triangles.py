from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from itertools import combinations


@dataclass(frozen=True)
class Triangle:
    symbols: tuple[str, str, str]
    assets: tuple[str, str, str]


def build_triangles(exchange_info: dict, max_triangles: int = 5000):
    markets = {}
    for s in exchange_info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        markets[(s["baseAsset"], s["quoteAsset"])] = s["symbol"]

    usdt_assets = sorted({base for base, quote in markets if quote == "USDT"})
    out = []
    for a, b in combinations(usdt_assets, 2):
        if (a, b) in markets and (a, "USDT") in markets and (b, "USDT") in markets:
            out.append(Triangle((markets[(a, "USDT")], markets[(a, b)], markets[(b, "USDT")]), ("USDT", a, b)))
        elif (b, a) in markets and (a, "USDT") in markets and (b, "USDT") in markets:
            out.append(Triangle((markets[(b, "USDT")], markets[(b, a)], markets[(a, "USDT")]), ("USDT", b, a)))
        if len(out) >= max_triangles:
            break
    return out


def _convert(asset_from, asset_to, qty, books, symbol_meta):
    if asset_from == asset_to:
        return qty, None
    candidates = []
    for symbol, meta in symbol_meta.items():
        base, quote = meta
        if base == asset_from and quote == asset_to:
            candidates.append((symbol, "sell"))
        elif base == asset_to and quote == asset_from:
            candidates.append((symbol, "buy"))
    for symbol, side in candidates:
        book = books.get(symbol, {})
        levels = book.get("bids" if side == "sell" else "asks") or []
        if not levels:
            continue
        remaining = qty
        result = Decimal("0")
        spent = Decimal("0")
        for raw_price, raw_qty in levels:
            price, level_qty = Decimal(str(raw_price)), Decimal(str(raw_qty))
            if price <= 0 or level_qty <= 0:
                continue
            if side == "sell":
                take = min(remaining, level_qty)
                result += take * price
                remaining -= take
                spent += take
            else:
                # For a BUY, qty is quote currency to spend. Consume ask levels by quote cost.
                take_base = min(level_qty, remaining / price)
                result += take_base
                cost = take_base * price
                remaining -= cost
                spent += cost
            if remaining <= Decimal("0"):
                break
        if remaining <= Decimal("0") and result > 0:
            return result, symbol
    return None, None


def evaluate_triangle(t: Triangle, books, fee_bps, slippage_bps, symbol_meta=None, notional_usdt=1.0):
    symbol_meta = symbol_meta or {}
    start = Decimal(str(notional_usdt))
    if start <= 0 or not all(s in books for s in t.symbols):
        return None

    amount = start
    used = []
    for i in range(3):
        src = t.assets[i]
        dst = t.assets[(i + 1) % 3]
        amount, symbol = _convert(src, dst, amount, books, symbol_meta)
        if amount is None:
            return None
        used.append(symbol)
        amount *= Decimal("1") - Decimal(str(fee_bps)) / Decimal("10000")
        if amount <= 0:
            return None

    gross_bps = (amount / start - Decimal("1")) * Decimal("10000")
    net_bps = gross_bps - Decimal(str(3 * slippage_bps))
    return net_bps, gross_bps, tuple(used), t.assets[1], t.assets[2]
