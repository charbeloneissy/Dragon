from decimal import Decimal

from src.dragon.risk import approved, risk_budget


def test_risk_budget_respects_account_and_max_notional():
    assert risk_budget(Decimal("1000"), 0.01, 25) == Decimal("10")


def test_risk_budget_fails_closed_below_exchange_minimum():
    assert risk_budget(Decimal("35.58"), 0.0015, 25, Decimal("5")) == Decimal("0")


def test_approved_enforces_minimum_trade_notional():
    assert approved(Decimal("20"), 5, Decimal("5"), 25, min_trade_notional=Decimal("5"))
    assert not approved(Decimal("20"), 5, Decimal("4.99"), 25, min_trade_notional=Decimal("5"))


def test_approved_rejects_large_notional():
    assert not approved(Decimal("30"), 20, Decimal("25.01"), 25, min_trade_notional=Decimal("5"))
