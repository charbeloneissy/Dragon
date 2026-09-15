from decimal import Decimal, ROUND_DOWN
import time
from .binance import BinanceClient


class ExecutionError(RuntimeError):
    pass


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _net_received(resp: dict, side: str, base: str, quote: str) -> tuple[Decimal, str]:
    base_qty = sum((Decimal(str(f.get("qty", "0"))) for f in resp.get("fills", [])), Decimal("0"))
    quote_qty = sum((Decimal(str(f.get("quoteQty", "0"))) for f in resp.get("fills", [])), Decimal("0"))
    asset = base if side == "BUY" else quote
    received = base_qty if side == "BUY" else quote_qty
    for f in resp.get("fills", []):
        if f.get("commissionAsset") == asset:
            received -= Decimal(str(f.get("commission", "0")))
    if received <= 0:
        raise ExecutionError(f"no positive net received amount for {base}/{quote} {side}")
    return received, asset


def execute_triangle(client: BinanceClient, path, first_asset: str, second_asset: str, start_usdt: Decimal, filters: dict, dry_run: bool = True):
    if dry_run:
        return {"dry_run": True, "path": path, "start_usdt": str(start_usdt)}
    amount = start_usdt
    legs = []
    for idx, symbol in enumerate(path):
        meta = filters[symbol]
        base, quote = meta["baseAsset"], meta["quoteAsset"]
        src = "USDT" if idx == 0 else legs[-1]["received_asset"]
        if src == quote:
            side = "BUY"
            if amount <= 0:
                raise ExecutionError(f"non-positive quote amount for {symbol}")
            resp = client.signed("POST", "/api/v3/order", {"symbol": symbol, "side": side, "type": "MARKET", "quoteOrderQty": str(amount), "newOrderRespType": "FULL"})
        elif src == base:
            side = "SELL"
            qty = floor_step(amount, Decimal(str(meta["stepSize"])))
            if qty < Decimal(str(meta["minQty"])):
                raise ExecutionError(f"quantity {qty} below minQty for {symbol}")
            resp = client.signed("POST", "/api/v3/order", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": format(qty, "f"), "newOrderRespType": "FULL"})
        else:
            raise ExecutionError(f"path asset mismatch at {symbol}: source={src}")
        if resp.get("status") != "FILLED":
            raise ExecutionError(f"{symbol} order not fully filled: status={resp.get('status')}")
        received, received_asset = _net_received(resp, side, base, quote)
        legs.append({"symbol": symbol, "side": side, "received": str(received), "received_asset": received_asset, "order_id": resp.get("orderId"), "response": resp})
        amount = received
    return {"dry_run": False, "legs": legs, "start_usdt": str(start_usdt), "final_asset": "USDT", "final_usdt": str(amount), "estimated_pnl_usdt": str(amount - start_usdt), "finished": True, "timestamp": time.time()}
