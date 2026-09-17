from decimal import Decimal
import asyncio
import time

from src.dragon.cross_exchange_futures import CrossExchangeFutures, Opportunity, Quote, Settings


def _engine(leverage=1, dynamic=True, compound=True):
    e = object.__new__(CrossExchangeFutures)
    e.settings = Settings(starting_balance=Decimal("5"), min_profit_usdt=Decimal("0.005"), leverage=leverage, quote_age_ms=1000, pair_skew_ms=500, poll_ms=250, leg_timeout_ms=1500, max_hold_ms=30000, live=True, dynamic_sizing=dynamic, compound_realized_pnl=compound)
    e.markets = {"a": {"BTC/USDT:USDT": {"limits": {"amount": {"min": Decimal("0.001")}, "cost": {"min": 0}}, "precision": {"amount": 3}}}, "b": {"BTC/USDT:USDT": {"limits": {"amount": {"min": Decimal("0.001")}, "cost": {"min": 0}}, "precision": {"amount": 3}}}}
    return e


def test_required_strategy_settings():
    s = Settings.from_env()
    assert s.starting_balance == Decimal("5")
    assert s.min_profit_usdt == Decimal("0.005")


def test_cross_exchange_profit_gate():
    e = _engine()
    now = int(time.time() * 1000)
    a = Quote("a", "BTC/USDT:USDT", Decimal("100000"), Decimal("100000"), Decimal("1"), Decimal("1"), now)
    b = Quote("b", "BTC/USDT:USDT", Decimal("102000"), Decimal("102000"), Decimal("1"), Decimal("1"), now)
    op = e.evaluate_pair(a, b)
    assert op is not None
    assert op.net_profit >= Decimal("0.005")


def test_profit_calculation_includes_all_costs():
    e = _engine()
    op = Opportunity("BTC/USDT:USDT", "a", "b", Decimal("100000"), Decimal("100100"), Decimal("0.001"), Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), int(time.time() * 1000))
    gross, fees, slippage, funding, net = e._profit_for_quantity(op, Decimal("0.001"))
    assert gross == Decimal("0.100")
    assert fees > 0 and slippage > 0 and funding > 0
    assert net == gross - fees - slippage - funding


def test_execution_sizing_uses_both_venues_free_margin_and_leverage():
    e = _engine(leverage=10)
    class Exchange:
        def __init__(self, free): self.free = free
        async def fetch_balance(self, _): return {"free": {"USDT": str(self.free)}}
    e.exchanges = {"a": Exchange(5), "b": Exchange(8)}
    op = Opportunity("BTC/USDT:USDT", "a", "b", Decimal("100"), Decimal("101"), Decimal("0.001"), Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), int(time.time() * 1000))
    assert asyncio.run(e._execution_quantity(op)) == Decimal("0.495")


def test_execution_rejects_minimum_lot_above_margin():
    e = _engine(leverage=1)
    class Exchange:
        async def fetch_balance(self, _): return {"free": {"USDT": "5"}}
    e.exchanges = {"a": Exchange(), "b": Exchange()}
    op = Opportunity("BTC/USDT:USDT", "a", "b", Decimal("100000"), Decimal("100100"), Decimal("0.001"), Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), int(time.time() * 1000))
    assert asyncio.run(e._execution_quantity(op)) == Decimal("0")


def test_no_triangular_strategy():
    assert "no triangles" in CrossExchangeFutures.__doc__.lower()
