import pytest
from dex_cross_exchange_runner import quote_latency_ms
from dragon.config import Config

def test_live_and_dry_run_cannot_both_be_enabled():
    with pytest.raises(ValueError): Config(dry_run=True, live_trading=True).validate()

def test_paper_quote_latency_has_safe_floor(monkeypatch):
    monkeypatch.setenv("DEX_MAX_QUOTE_LATENCY_MS", "500")
    assert quote_latency_ms(False) == 1500

def test_live_quote_latency_remains_configurable(monkeypatch):
    monkeypatch.setenv("DEX_MAX_QUOTE_LATENCY_MS", "500")
    assert quote_latency_ms(True) == 500
