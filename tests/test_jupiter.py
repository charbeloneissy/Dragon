import pytest

from src.dragon.dex_nonevm import NonEvmDexAdapter


SOL = "So11111111111111111111111111111111111111112"
USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def test_jupiter_quote_uses_bearer_auth(monkeypatch):
    monkeypatch.setenv("JUPITER_API_KEY", "test-jupiter-key")
    adapter = NonEvmDexAdapter(["solana"])
    captured = {}

    def fake_get_json(url, headers=None):
        captured["url"] = url
        captured["headers"] = headers
        return {"outAmount": "123456", "routePlan": [{"swapInfo": {}}]}

    monkeypatch.setattr(adapter, "_get_json", fake_get_json)
    quote = adapter.quote(
        chain="solana", venue="Jupiter", sell_denom=SOL, buy_denom=USDC,
        sell_amount=1_000_000_000, slippage_bps=50,
    )
    assert quote.buy_amount == 123456
    assert captured["headers"] == {"Authorization": "Bearer test-jupiter-key"}
    assert "inputMint=" + SOL in captured["url"]
    assert "outputMint=" + USDC in captured["url"]


def test_jupiter_quote_requires_key(monkeypatch):
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    adapter = NonEvmDexAdapter(["solana"])
    with pytest.raises(RuntimeError, match="JUPITER_API_KEY"):
        adapter.quote(
            chain="solana", venue="Jupiter", sell_denom=SOL, buy_denom=USDC,
            sell_amount=1_000_000_000,
        )


def test_solana_is_not_enabled_without_family_config(monkeypatch):
    monkeypatch.delenv("NONEVM_CHAINS", raising=False)
    with pytest.raises(RuntimeError):
        NonEvmDexAdapter()
