from decimal import Decimal

from src.dragon.dex_0x import DexExecution
from src.dragon.dex import DexQuote
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine


class HybridAdapter:
    def quote_single_source(self, *, sell_token, buy_token, sell_amount, source, **_kwargs):
        if sell_token == "QUOTE":
            multiplier = Decimal("2") if source == "A" else Decimal("1")
        else:
            multiplier = Decimal("0.6") if source == "B" else Decimal("0.55")
        buy_amount = int(Decimal(sell_amount) * multiplier)
        quote = DexQuote(
            chain="8453", venue=source, sell_token=sell_token, buy_token=buy_token,
            sell_amount=Decimal(sell_amount), buy_amount=Decimal(buy_amount),
            gas_native=Decimal("1000000000000000"), gas_quote=Decimal("0"),
            fee_bps=Decimal("0"), slippage_bps=Decimal("50"), latency_ms=Decimal("1"),
        )
        execution = DexExecution(
            chain_id=8453, venue=source, source=source,
            to="0x0000000000000000000000000000000000000001", data="0xdeadbeef",
            value=0, gas=100000, gas_price=1, sell_token=sell_token,
            buy_token=buy_token, sell_amount=sell_amount, buy_amount=buy_amount,
            allowance_target="0x0000000000000000000000000000000000000002", issues={},
        )
        return quote, execution


def test_flash_fee_applies_only_to_borrowed_amount_when_compounding():
    engine = DexCrossExchangeEngine(
        HybridAdapter(), ["A", "B"], min_profit=Decimal("0.005"),
        native_to_quote_rate=Decimal("1"), flash_loan_enabled=True,
        flash_loan_fee_bps=Decimal("100"), min_net_bps=Decimal("0"),
    )
    opportunities = engine.scan_once(
        chain_id=8453, quote_token="QUOTE", base_token="BASE",
        quote_amount=1_100_000, taker="0x1", compound_amount=100_000,
    )
    assert opportunities
    opportunity = opportunities[0]
    assert opportunity.compound_amount == 100_000
    assert opportunity.flash_loan_amount == 1_000_000
    assert opportunity.flash_loan_fee_quote == Decimal("0.01")


def test_compound_amount_must_leave_positive_flash_loan():
    engine = DexCrossExchangeEngine(HybridAdapter(), ["A", "B"])
    try:
        engine.scan_once(
            chain_id=8453, quote_token="QUOTE", base_token="BASE",
            quote_amount=100, taker="0x1", compound_amount=100,
        )
    except ValueError as exc:
        assert "below quote_amount" in str(exc)
    else:
        raise AssertionError("compound amount must not consume the entire trade")
