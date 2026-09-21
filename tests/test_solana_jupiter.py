from __future__ import annotations

import httpx
import pytest

from src.dragon.solana_jupiter import JupiterQuoteAdapter


def test_jupiter_quote_requires_configuration(monkeypatch):
    monkeypatch.delenv("JUPITER_API_KEY", raising=False)
    monkeypatch.delenv("JUPITER_QUOTE_ALLOW_UNKEYED", raising=False)
    adapter = JupiterQuoteAdapter()
    assert adapter.enabled is False


def test_jupiter_quote_rejects_bad_amount(monkeypatch):
    monkeypatch.setenv("JUPITER_QUOTE_ALLOW_UNKEYED", "true")
    adapter = JupiterQuoteAdapter(base_url="https://example.invalid")
    with pytest.raises(ValueError):
        adapter.quote(input_mint="A", output_mint="B", amount=0)


def test_jupiter_quote_parses_response(monkeypatch):
    monkeypatch.setenv("JUPITER_QUOTE_ALLOW_UNKEYED", "true")

    class MockClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params, headers):
            request = httpx.Request("GET", url)
            return httpx.Response(
                200,
                request=request,
                json={
                    "inputMint": "A",
                    "outputMint": "B",
                    "inAmount": "1000",
                    "outAmount": "1200",
                    "priceImpactPct": "0.12",
                    "contextSlot": 123,
                    "routePlan": [{"swapInfo": {"label": "test"}}],
                },
            )

    monkeypatch.setattr("src.dragon.solana_jupiter.httpx.Client", MockClient)
    quote = JupiterQuoteAdapter(base_url="https://example.invalid").quote(
        input_mint="A", output_mint="B", amount=1000
    )
    assert quote.out_amount == 1200
    assert quote.context_slot == 123
    assert quote.route_plan
    assert quote.age_ms >= 0
