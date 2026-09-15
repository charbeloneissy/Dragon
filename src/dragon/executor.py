from decimal import Decimal, ROUND_DOWN
import time
from .binance import BinanceClient, BinanceError


class ExecutionError(RuntimeError):
    pass


def floor_step(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _net_received(resp: dict, side: str, base: str, quote: str):
    fills = resp.get("fills", [])
    base_qty = sum((Decimal(str(f.get("qty", "0"))) for f in fills), Decimal("0"))
    quote_qty = sum((Decimal(str(f.get("quoteQty", "0"))) for f in fills), Decimal("0"))
    asset = base if side == "BUY" else quote
    received = base_qty if side == "BUY" else quote_qty
    for f in fills:
        if f.get("commissionAsset") == asset:
            received -= Decimal(str(f.get("commission", "0")))
    if received <= 0:
        raise ExecutionError(f"no positive net received amount for {base}/{quote} {side}")
    return received, asset


def _validate_market(meta, side, amount):
    step = Decimal(str(meta.get("stepSize", "0")))
    min_qty = Decimal(str(meta.get("minQty", "0")))
    min_notional = Decimal(str(meta.get("minNotional", "0")))
    if side == "SELL":
        qty = floor_step(amount, step)
        if qty < min_qty:
            raise ExecutionError(f"quantity {qty} below minQty {min_qty}")
        return qty
    if min_notional > 0 and amount < min_notional:
        raise ExecutionError(f"quote amount {amount} below minNotional {min_notional}")
    return amount


def execute_triangle(client: BinanceClient, path, start_asset, first_asset, start_usdt, filters, dry_run=False):
    if dry_run:
        return {"dry_run": True, "path": path, "start_usdt": str(start_usdt)}
    if len(path) != 3:
        raise ExecutionError(f"triangle must contain exactly 3 symbols: {path}")

    amount = Decimal(str(start_usdt))
    source_asset = start_asset or "USDT"
    legs = []
    started = time.time()

    for symbol in path:
        if symbol not in filters:
            raise ExecutionError(f"missing exchange filters for {symbol}")
        meta = filters[symbol]
        base, quote = meta["baseAsset"], meta["quoteAsset"]
        if source_asset == quote:
            side = "BUY"
            quote_amount = _validate_market(meta, side, amount)
            params = {"symbol": symbol, "side": side, "type": "MARKET", "quoteOrderQty": format(quote_amount, "f"), "newOrderRespType": "FULL"}
        elif source_asset == base:
            side = "SELL"
            qty = _validate_market(meta, side, amount)
            params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": format(qty, "f"), "newOrderRespType": "FULL"}
        else:
            raise ExecutionError(f"path asset mismatch at {symbol}: source={source_asset}, base={base}, quote={quote}")

        try:
            resp = client.signed("POST", "/api/v3/order", params)
        except BinanceError as exc:
            raise ExecutionError(f"order rejected {symbol} {side}: {exc}") from exc
        status = resp.get("status")
        if status != "FILLED":
            raise ExecutionError(f"{symbol} order not fully filled: status={status}, orderId={resp.get('orderId')}")
        received, received_asset = _net_received(resp, side, base, quote)
        legs.append({"symbol": symbol, "side": side, "received": str(received), "received_asset": received_asset, "order_id": resp.get("orderId"), "status": status, "response": resp})
        source_asset = received_asset
        amount = received

    if source_asset != "USDT":
        raise ExecutionError(f"triangle did not finish in USDT: {source_asset}")
    return {
        "dry_run": False,
        "legs": legs,
        "start_usdt": str(start_usdt),
        "final_asset": source_asset,
        "final_usdt": str(amount),
        "realized_pnl_usdt": str(amount - Decimal(str(start_usdt))),
        "finished": True,
        "duration_ms": round((time.time() - started) * 1000, 2),
        "timestamp": time.time(),
    }
