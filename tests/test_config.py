import importlib

import pytest

import dex_cross_exchange_runner as runner
from src.dragon.chains import get_spec


def test_enabled_evm_chains_defaults_to_base(monkeypatch):
    monkeypatch.delenv("DEX_CHAINS", raising=False)
    assert runner._enabled_evm_chains() == [8453]


def test_enabled_evm_chains_filters_unknown_and_dedupes(monkeypatch):
    monkeypatch.setenv("DEX_CHAINS", "8453, 999999, 8453, 1")
    assert runner._enabled_evm_chains() == [8453, 1]


def test_enabled_nonevm_parses_and_lowercases(monkeypatch):
    monkeypatch.setenv("NONEVM_CHAINS", "Tron, cosmos ,")
    assert runner._enabled_nonevm() == ["tron", "cosmos"]


def test_quote_token_default_is_six_decimal_stablecoin(monkeypatch):
    monkeypatch.delenv("DEX_QUOTE_TOKENS", raising=False)
    token, decimals = runner._quote_token_for(8453)
    assert token.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    assert decimals == 6


def test_quote_token_override_is_per_chain(monkeypatch):
    monkeypatch.setenv("DEX_QUOTE_TOKENS", "8453:0x0000000000000000000000000000000000000001:8")
    token, decimals = runner._quote_token_for(8453)
    assert token == "0x0000000000000000000000000000000000000001"
    assert decimals == 8


def test_base_tokens_default_include_wrapped_native(monkeypatch):
    monkeypatch.delenv("DEX_PAPER_BASE_TOKENS", raising=False)
    tokens = runner._base_tokens_for(8453)
    assert tokens == ["0x4200000000000000000000000000000000000006"]


def test_base_tokens_override(monkeypatch):
    monkeypatch.setenv("DEX_PAPER_BASE_TOKENS", "0x0000000000000000000000000000000000000002")
    assert runner._base_tokens_for(8453) == ["0x0000000000000000000000000000000000000002"]


def test_validate_evm_address_rejects_bad_input():
    with pytest.raises(ValueError):
        runner.validate_evm_address("X", "not-an-address")
    with pytest.raises(ValueError):
        runner.validate_evm_address("X", "0x" + "zz" * 20)


def test_env_decimal_roundtrip(monkeypatch):
    monkeypatch.setenv("SOME_DEC", "1.25")
    assert str(runner.env_decimal("SOME_DEC", "0")) == "1.25"


def test_every_enabled_default_chain_has_registry_spec():
    for chain_id in runner.PAPER_BASE_TOKENS:
        assert get_spec(chain_id) is not None
    for chain_id in runner.DEFAULT_QUOTE_TOKENS:
        assert get_spec(chain_id) is not None


def test_quote_units_are_scaled_per_token_decimals():
    assert runner.quote_units(runner.Decimal("100"), 6) == 100_000_000
    assert runner.quote_units(runner.Decimal("100"), 18) == 100_000000000000000000


def test_quote_units_reject_non_positive():
    with pytest.raises(ValueError):
        runner.quote_units(runner.Decimal("0"), 6)
