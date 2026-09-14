from decimal import Decimal, ROUND_DOWN
import time
from .binance import BinanceClient, BinanceError


class ExecutionError(RuntimeError):
    pass


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def execute_triangle(client: BinanceClient, path, first_asset: str, second_asset: str, start_usdt: Decimal, filters: dict, dry_run: bool = True):
    if dry_run:
        return {"dry_run": True, "path": path, "start_usdt": str(start_usdt)}

    amount = start_usdt
    legs = []
    # Each path symbol is interpreted from exchangeInfo base/quote metadata.
    for idx, symbol in enumerate(path):
        meta = filters[symbol]
        base, quote = meta["baseAsset"], meta["quoteAsset"]
        if idx == 0:
            src = "USDT"
        else:
            src = legs[-1]["received_asset"]
        if src == quote:
            side = "BUY"
            quantity = None
            quote_amount = amount
            resp = client.signed("POST", "/api/v3/order", {
                "symbol": symbol, "side": side, "type": "MARKET",
                "quoteOrderQty": str(quote_amount), "newOrderRespType": "FULL",
            })
        elif src == base:
            side = "SELL"
            qty = floor_step(amount, Decimal(str(meta["stepSize"])))
            if qty <= 0:
                raise ExecutionError(f"quantity too small for {symbol}")
            resp = client.signed("POST", "/api/v3/order", {
                "symbol": symbol, "side": side, "type": "MARKET",
                "quantity": format(qty, "f"), "newOrderRespType": "FULL",
            })
        else:
            raise ExecutionError(f"path asset mismatch at {symbol}: source={src}")
        status = resp.get("status")
        if status != "FILLED":
            raise ExecutionError(f"{symbol} order not fully filled: {resp}")
        received = Decimal("0")
        for fill in resp.get("fills", []):
            received += Decimal(str(fill.get("qty", "0")))
        received_asset = base if side == "BUY" else quote
        legs.append({"symbol": symbol, "side": side, "received": str(received), "received_asset": received_asset, "response": resp})
        amount = received
    return {"dry_run": False, "legs": legs, "finished": True, "timestamp": time.time()}
