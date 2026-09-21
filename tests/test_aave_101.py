from decimal import Decimal

from src.dragon.aave_101 import Aave101Model, AaveAction, AavePositionState, AaveReserveState


def test_aave_101_reserve_flags_match_mcp_safety():
    model = Aave101Model()
    assert model.can_execute_lending_action(AaveReserveState(frozen=True), AaveAction.SUPPLY) is False
    assert model.can_execute_lending_action(AaveReserveState(frozen=True), AaveAction.BORROW) is False
    assert model.can_execute_lending_action(AaveReserveState(frozen=True), AaveAction.WITHDRAW) is True
    assert model.can_execute_lending_action(AaveReserveState(frozen=True), AaveAction.REPAY) is True
    assert model.can_execute_lending_action(AaveReserveState(paused=True), AaveAction.WITHDRAW) is False


def test_flash_loan_is_not_treated_as_collateralized_lending():
    model = Aave101Model()
    assert model.context(AaveAction.FLASH_LOAN) == "atomic_liquidity"
    import pytest
    with pytest.raises(ValueError):
        model.can_execute_lending_action(AaveReserveState(paused=True), AaveAction.FLASH_LOAN)


def test_health_factor_and_collateral_are_position_properties():
    model = Aave101Model()
    safe = AavePositionState(health_factor=Decimal("1.8"), collateral_enabled=True)
    risky = AavePositionState(health_factor=Decimal("0.9"), collateral_enabled=True)
    supplied_not_collateral = AavePositionState(health_factor=Decimal("0"), collateral_enabled=False)
    assert model.has_borrowing_power(safe) is True
    assert model.is_liquidatable(risky) is True
    assert model.has_borrowing_power(supplied_not_collateral) is False
