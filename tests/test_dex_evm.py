from decimal import Decimal
from types import SimpleNamespace

from src.dragon.dex_evm import EvmDexAdapter


def test_native_rate_falls_back_when_first_venue_has_no_pair():
    adapter = object.__new__(EvmDexAdapter)
    adapter._native_rate_cache = {}
    adapter.sources = lambda _chain_id: ("empty", "working")

    def quote_single_source(*, source, **_kwargs):
        if source == "empty":
            raise RuntimeError("no liquidity")
        return SimpleNamespace(buy_amount=250, sell_amount=100), None

    adapter.quote_single_source = quote_single_source

    assert adapter.native_to_quote_rate(
        chain_id=8453,
        quote_token="0x0000000000000000000000000000000000000001",
        sell_amount_native=100,
        taker="0x000000000000000000000000000000000000dEaD",
    ) == Decimal("2.5")
