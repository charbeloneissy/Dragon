from decimal import Decimal

from src.dragon.dex import DexQuote
from src.dragon.dex_0x import DexExecution
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine


class FakeAdapter:
    def quote_single_source(self, *, sell_token, buy_token, sell_amount, source, **_kwargs):
        if source == "A":
            buy_amount = 1_010_000 if sell_token == "QUOTE" else 990_000
        else:
            buy_amount = 1_000_000 if sell_token == "QUOTE" else 1_020_000
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
        quote = DexQuote(
            chain="8453", venue=source, sell_token=sell_token, buy_token=buy_token,
            sell_amount=Decimal(sell_amount), buy_amount=Decimal(buy_amount),
            gas_native=Decimal("0"), gas_quote=Decimal("0"), fee_bps=Decimal("0"),
            slippage_bps=Decimal("50"), latency_ms=Decimal("1"),
        )
        return quote, execution


def test_cross_dex_requires_two_sources():
    engine = DexCrossExchangeEngine(FakeAdapter(), ["A"], min_profit=Decimal("0.005"))
    try:
        engine.scan_once(chain_id=8453, quote_token="QUOTE", base_token="BASE", quote_amount=1_000_000, taker="0x1")
    except ValueError as exc:
        assert "at least two" in str(exc)
    else:
        raise AssertionError("single-source configuration must be rejected")


def test_cross_dex_returns_only_cross_source_opportunities():
    engine = DexCrossExchangeEngine(
        FakeAdapter(), ["A", "B"], min_profit=Decimal("0.005"), native_to_quote_rate=Decimal("1")
    )
    opportunities = engine.scan_once(
        chain_id=8453,
        quote_token="QUOTE",
        base_token="BASE",
        quote_amount=1_000_000,
        taker="0x1",
    )
    assert opportunities
    assert all(item.buy_source != item.sell_source for item in opportunities)
    assert all(item.net_profit_quote >= Decimal("0.005") for item in opportunities)


def test_zero_gas_quote_falls_back_to_native_gas():
    engine = DexCrossExchangeEngine(
        FakeAdapter(), ["A", "B"], min_profit=Decimal("0.005"), native_to_quote_rate=Decimal("1")
    )
    opportunities = engine.scan_once(
        chain_id=8453,
        quote_token="QUOTE",
        base_token="BASE",
        quote_amount=1_000_000,
        taker="0x1",
    )
    assert opportunities
    assert all(item.gas_cost_quote > Decimal("0") for item in opportunities)
