from src.dragon.dex_composite import CompositeDexAdapter


class FakeDirect:
    def sources(self, chain_id):
        return ("Uniswap_V3", "Aerodrome")


def test_zero_x_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("ZEROX_API_KEY", "test-key")
    monkeypatch.setenv("DEX_0X_AGGREGATED", "true")
    monkeypatch.delenv("DEX_0X_OPT_IN", raising=False)

    assert CompositeDexAdapter(FakeDirect()).sources(8453) == ("Uniswap_V3", "Aerodrome")


def test_zero_x_can_be_enabled_explicitly(monkeypatch):
    monkeypatch.setenv("ZEROX_API_KEY", "test-key")
    monkeypatch.setenv("DEX_0X_AGGREGATED", "true")
    monkeypatch.setenv("DEX_0X_OPT_IN", "true")

    assert CompositeDexAdapter(FakeDirect()).sources(8453) == (
        "Uniswap_V3", "Aerodrome", "0x:AGGREGATED"
    )
