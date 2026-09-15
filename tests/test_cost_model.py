from decimal import Decimal

from src.dragon.cost_model import gas_to_bps, net_opportunity


def test_gas_is_converted_to_basis_points():
    assert gas_to_bps(Decimal("0.50"), Decimal("100")) == Decimal("50")


def test_net_edge_includes_fee_slippage_and_gas():
    result = net_opportunity(
        Decimal("100"),
        fee_bps=Decimal("10"),
        slippage_bps=Decimal("15"),
        gas_quote=Decimal("0.50"),
        notional_quote=Decimal("100"),
    )
    assert result.net_bps == Decimal("25")
    assert result.costs.total_bps == Decimal("75")


def test_zero_notional_with_gas_fails_closed():
    result = net_opportunity(Decimal("100"), gas_quote=Decimal("1"), notional_quote=Decimal("0"))
    assert result.net_bps == Decimal("-Infinity")
