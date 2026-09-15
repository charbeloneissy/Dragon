from decimal import Decimal

import pytest

from dragon.executor import ExecutionError, _reconcile_and_recover, _validate_market


def test_sell_quantity_is_floored_to_market_step():
    meta = {"stepSize": "0.001", "minQty": "0.01", "maxQty": "10", "minNotional": "5", "maxNotional": "0"}
    assert _validate_market(meta, "SELL", Decimal("1.23456")) == Decimal("1.234")


def test_sell_quantity_below_minimum_is_rejected():
    meta = {"stepSize": "0.001", "minQty": "0.01", "maxQty": "10", "minNotional": "5", "maxNotional": "0"}
    with pytest.raises(ExecutionError, match="below minQty"):
        _validate_market(meta, "SELL", Decimal("0.0099"))


def test_buy_quote_amount_enforces_notional_bounds():
    meta = {"stepSize": "0.001", "minQty": "0.01", "maxQty": "10", "minNotional": "10", "maxNotional": "100"}
    with pytest.raises(ExecutionError, match="below minNotional"):
        _validate_market(meta, "BUY", Decimal("9"))
    with pytest.raises(ExecutionError, match="above maxNotional"):
        _validate_market(meta, "BUY", Decimal("101"))


class FakeClient:
    def __init__(self):
        self.balances = {"USDT": Decimal("100"), "AAA": Decimal("1"), "BBB": Decimal("0")}
        self.orders = []

    def account(self):
        return {"balances": [{"asset": k, "free": str(v)} for k, v in self.balances.items()]}

    def new_market_order(self, symbol, side, *, quantity=None, quote_order_qty=None):
        self.orders.append((symbol, side, quantity, quote_order_qty))
        if symbol == "AAAUSDT" and side == "SELL":
            self.balances["AAA"] -= quantity
            self.balances["USDT"] += quantity * Decimal("10")
        return {
            "status": "FILLED",
            "orderId": len(self.orders),
            "fills": [{"qty": str(quantity), "quoteQty": str(quantity * Decimal("10")), "commission": "0", "commissionAsset": "USDT"}],
        }

    def order(self, symbol, order_id):
        raise AssertionError("not needed for this test")


def test_reconcile_only_recovers_positive_cycle_deltas():
    client = FakeClient()
    filters = {
        "AAAUSDT": {
            "baseAsset": "AAA",
            "quoteAsset": "USDT",
            "stepSize": "0.001",
            "minQty": "0.01",
            "maxQty": "100",
            "minNotional": "5",
            "maxNotional": "0",
        }
    }
    baseline = {"USDT": Decimal("100"), "AAA": Decimal("0.5"), "BBB": Decimal("0")}
    client.balances["AAA"] = Decimal("0.75")
    _reconcile_and_recover(client, filters, baseline, {"USDT", "AAA", "BBB"})
    assert client.balances["AAA"] == Decimal("0.5")
    assert client.balances["USDT"] == Decimal("102.5")
