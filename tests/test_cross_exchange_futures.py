from decimal import Decimal
import time

from src.dragon.cross_exchange_futures import CrossExchangeFutures, Quote, Settings


def test_required_strategy_settings():
    s = Settings.from_env()
    assert s.starting_balance == Decimal("5")
    assert s.min_profit_usdt == Decimal("0.005")


def test_cross_exchange_profit_gate():
    e = object.__new__(CrossExchangeFutures)
    e.settings = Settings(
        starting_balance=Decimal("5"),
        min_profit_usdt=Decimal("0.005"),
        leverage=1,
        quote_age_ms=1000,
        pair_skew_ms=500,
        poll_ms=250,
        leg_timeout_ms=1500,
        max_hold_ms=30000,
        live=False,
    )
    e.markets = {
        "a": {"BTC/USDT:USDT": {"limits": {"amount": {"min": 0.001}, "cost": {"min": 0}}, "precision": {"amount": 3}}},
        "b": {"BTC/USDT:USDT": {"limits": {"amount": {"min": 0.001}, "cost": {"min": 0}}, "precision": {"amount": 3}}},
    }
    now = int(time.time() * 1000)
    a = Quote("a", "BTC/USDT:USDT", Decimal("100000"), Decimal("100000"), Decimal("1"), Decimal("1"), now)
    b = Quote("b", "BTC/USDT:USDT", Decimal("102000"), Decimal("102000"), Decimal("1"), Decimal("1"), now)
    op = e.evaluate_pair(a, b)
    assert op is not None
    assert op.net_profit >= Decimal("0.005")


def test_no_triangular_strategy():
    assert "no triangle" in CrossExchangeFutures.__doc__.lower()
