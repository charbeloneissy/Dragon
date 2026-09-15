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
    max_qty = Decimal(str(meta.get("maxQty", "0")))
    min_notional = Decimal(str(meta.get("minNotional", "0")))
    max_notional = Decimal(str(meta.get("maxNotional", "0")))
    if side == "SELL":
        qty = floor_step(amount, step)
        if qty < min_qty:
            raise ExecutionError(f"quantity {qty} below minQty {min_qty}")
        if max_qty > 0 and qty > max_qty:
            qty = floor_step(max_qty, step)
        return qty
    if min_notional > 0 and amount < min_notional:
        raise ExecutionError(f"quote amount {amount} below minNotional {min_notional}")
    if max_notional > 0 and amount > max_notional:
        raise ExecutionError(f"quote amount {amount} above maxNotional {max_notional}")
    return amount


def _find_recovery_symbol(filters, asset):
    direct = []
    for symbol, meta in filters.items():
        if meta.get("baseAsset") == asset and meta.get("quoteAsset") == "USDT":
            direct.append((symbol, "SELL"))
        elif meta.get("baseAsset") == "USDT" and meta.get("quoteAsset") == asset:
            direct.append((symbol, "BUY"))
    return direct[0] if direct else (None, None)


def _recover_to_usdt(client, filters, asset, amount):
    if asset == "USDT" or amount <= 0:
        return {"recovered": True, "asset": asset, "amount": str(amount)}
    symbol, side = _find_recovery_symbol(filters, asset)
    if not symbol:
        raise ExecutionError(f"cannot recover {amount} {asset} to USDT: no direct USDT market")
    meta = filters[symbol]
    try:
        if side == "SELL":
            qty = _validate_market(meta, "SELL", amount)
            resp = client.new_market_order(symbol, "SELL", quantity=qty)
        else:
            quote_amount = _validate_market(meta, "BUY", amount)
            resp = client.new_market_order(symbol, "BUY", quote_order_qty=quote_amount)
    except BinanceError as exc:
        raise ExecutionError(f"recovery order rejected {symbol} {side}: {exc}") from exc
    if resp.get("status") != "FILLED":
        order_id = resp.get("orderId")
        if order_id:
            try:
                resp = client.order(symbol, int(order_id))
            except BinanceError as exc:
                raise ExecutionError(f"recovery status unknown for {symbol} order {order_id}: {exc}") from exc
    if resp.get("status") != "FILLED":
        raise ExecutionError(f"recovery not fully filled: {symbol} orderId={resp.get('orderId')} status={resp.get('status')}")
    received, received_asset = _net_received(resp, side, meta["baseAsset"], meta["quoteAsset"])
    if received_asset != "USDT":
        raise ExecutionError(f"recovery did not finish in USDT: received {received_asset}")
    return {"recovered": True, "symbol": symbol, "side": side, "amount_usdt": str(received), "order_id": resp.get("orderId")}


def _order_filled_or_reconcile(client, symbol, order_id, initial_response):
    status = initial_response.get("status")
    if status == "FILLED":
        return initial_response
    if not order_id:
        raise ExecutionError(f"order status unknown for {symbol}: missing orderId")
    try:
        latest = client.order(symbol, int(order_id))
    except BinanceError as exc:
        raise ExecutionError(f"order status unknown for {symbol} order {order_id}: {exc}") from exc
    if latest.get("status") != "FILLED":
        raise ExecutionError(f"{symbol} order not fully filled: status={latest.get('status')}, orderId={order_id}")
    return latest


def execute_triangle(client: BinanceClient, path, start_asset, first_asset, start_usdt, filters, dry_run=False):
    if dry_run:
        return {"dry_run": True, "path": path, "start_usdt": str(start_usdt)}
    if len(path) != 3:
        raise ExecutionError(f"triangle must contain exactly 3 symbols: {path}")

    amount = Decimal(str(start_usdt))
    source_asset = start_asset or "USDT"
    legs = []
    started = time.time()

    try:
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

            resp = _order_filled_or_reconcile(client, symbol, resp.get("orderId"), resp)
            received, received_asset = _net_received(resp, side, base, quote)
            legs.append({
                "symbol": symbol,
                "side": side,
                "received": str(received),
                "received_asset": received_asset,
                "order_id": resp.get("orderId"),
                "status": resp.get("status"),
                "executed_qty": str(resp.get("executedQty", "0")),
                "quote_qty": str(resp.get("cummulativeQuoteQty", "0")),
                "fills": resp.get("fills", []),
            })
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
    except ExecutionError as exc:
        # At this point every completed leg has already been recorded locally.
        # Flatten the currently held intermediate asset so a failed later leg
        # does not silently leave the account exposed to market direction.
        if source_asset != "USDT" and amount > 0:
            recovery = _recover_to_usdt(client, filters, source_asset, amount)
            raise ExecutionError(f"{exc}; exposure recovered: {recovery}") from exc
        raise
