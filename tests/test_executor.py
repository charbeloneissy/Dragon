from decimal import Decimal

import pytest

from dragon.executor import ExecutionError, _validate_market


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
