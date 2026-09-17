from decimal import Decimal

from src.dragon.dex_0x import DexExecution
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine


class FakeAdapter:
    def quote_single_source(self, *, sell_token, buy_token, sell_amount, source, **_kwargs):
        # Source A is cheaper for QUOTE -> BASE; source B is better on BASE -> QUOTE.
        if source == "A":
            buy_amount = 101000 if sell_token == "QUOTE" else 990000
        else:
            buy_amount = 100000 if sell_token == "QUOTE" else 102000
        execution = DexExecution(
            chain_id=8453,
            venue="0x",
            source=source,
            to="0x0000000000000000000000000000000000000001",
            data="0xdeadbeef",
            value=0,
            gas=100000,
            gas_price=1,
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            buy_amount=buy_amount,
            allowance_target=None,
            issues={},
        )
        return type("Q", (), {"gas_quote": Decimal("0")})(), execution


def test_cross_dex_requires_two_sources():
    engine = DexCrossExchangeEngine(FakeAdapter(), ["A"], min_profit=Decimal("0.005"))
    try:
        engine.scan_once(chain_id=8453, quote_token="QUOTE", base_token="BASE", quote_amount=100000, taker="0x1")
    except ValueError as exc:
        assert "at least two" in str(exc)
    else:
        raise AssertionError("single-source configuration must be rejected")


def test_cross_dex_returns_only_cross_source_opportunities():
    engine = DexCrossExchangeEngine(FakeAdapter(), ["A", "B"], min_profit=Decimal("0.005"))
    opportunities = engine.scan_once(
        chain_id=8453,
        quote_token="QUOTE",
        base_token="BASE",
        quote_amount=100000,
        taker="0x1",
    )
    assert opportunities
    assert all(item.buy_source != item.sell_source for item in opportunities)
    assert all(item.net_profit_quote >= Decimal("0.005") for item in opportunities)
