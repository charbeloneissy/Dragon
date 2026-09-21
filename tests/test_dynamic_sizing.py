from decimal import Decimal as D

import pytest

from src.dragon.brains.economic.dynamic_sizing import (
    AdaptiveSizingEngine,
    ImpactFingerprint,
    SizeQuote,
)


def test_finds_local_profit_peak_without_linear_scan():
    calls = []

    def quote(q):
        calls.append(q)
        # Artificial concave net surface with maximum near 40.
        net = D("0.020") - (q - D("40")) ** 2 / D("100000")
        return SizeQuote(
            size=q,
            gross_spread=max(D("0"), net + D("0.002")),
            dex_fees=D("0.001"),
            flash_fee=D("0.0005"),
            gas=D("0.0002"),
            slippage=D("0.0003"),
            execution_probability=D("1"),
            quote_version="v1",
        )

    engine = AdaptiveSizingEngine(max_quote_calls=9, refinement_rounds=2)
    result = engine.optimize(min_size=D("1"), max_size=D("100"), quote_fn=quote)

    assert result["profitable"] is True
    assert D("25") <= result["size"] <= D("55")
    assert len(calls) <= 9
    assert result["quote_calls"] == len(calls)


def test_cache_reuses_valid_fingerprint():
    calls = []

    def quote(q):
        calls.append(q)
        return SizeQuote(q, D("1"), D("0.1"), D("0.01"), D("0.01"), q / D("10000"), D("1"), "v2")

    fp = ImpactFingerprint(8453, "a", "b", "USDC", "WETH", D("1000"), D("900"), D("1"), D("1.01"), D("5"), D("5"), D("0.02"))
    engine = AdaptiveSizingEngine(max_quote_calls=5, refinement_rounds=0)
    first = engine.optimize(min_size=D("1"), max_size=D("100"), quote_fn=quote, fingerprint=fp)
    before = len(calls)
    second = engine.optimize(min_size=D("1"), max_size=D("100"), quote_fn=quote, fingerprint=fp)

    assert first["status"] == "optimized"
    assert second["status"] == "cached"
    assert len(calls) == before
    assert second["size"] == first["size"]


def test_invalid_range_is_rejected():
    engine = AdaptiveSizingEngine()
    with pytest.raises(ValueError):
        engine.optimize(min_size=D("0"), max_size=D("10"), quote_fn=lambda q: None)
