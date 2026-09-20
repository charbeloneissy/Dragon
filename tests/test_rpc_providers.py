import os

import pytest

from src.dragon import rpc_providers
from src.dragon.chains import get_spec, rpc_urls


@pytest.fixture(autouse=True)
def clear_provider_env(monkeypatch):
    for name in [
        "ALCHEMY_API_KEY", "ALCHEMY_KEY", "INFURA_API_KEY", "INFURA_PROJECT_ID",
        "QUICKNODE_API_KEY", "QUICKNODE_TOKEN", "QUICKNODE_ENDPOINT", "QUICKNODE_HOST",
        "QUICKNODE_CHAIN_ID", "QUICKNODE_CHAIN", "DEX_RPC_URL", "DEX_RPC_URLS",
        "BASE_RPC_URL", "ARBITRUM_RPC_URL", "ALCHEMY_BASE_URL",
        "INFURA_BASE_URL", "QUICKNODE_ARBITRUM_URL",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_no_keys_uses_public_fallback():
    assert rpc_urls(get_spec(8453)) == ["https://base-rpc.publicnode.com"]


def test_alchemy_and_infura_are_preferred_per_chain(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "ALC")
    monkeypatch.setenv("INFURA_API_KEY", "INF")
    urls = rpc_urls(get_spec(42161))
    assert urls[0] == "https://arb-mainnet.g.alchemy.com/v2/ALC"
    assert urls[1] == "https://arbitrum-mainnet.infura.io/v3/INF"
    assert "publicnode" not in " ".join(urls)


def test_quicknode_only_applies_to_declared_chain(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "ALC")
    monkeypatch.setenv("QUICKNODE_API_KEY", "QN")
    monkeypatch.setenv("QUICKNODE_ENDPOINT", "base-mainnet")
    # No QUICKNODE_CHAIN_ID: the single host is not reused on every chain.
    assert not any("quiknode" in u for u in rpc_urls(get_spec(8453)))
    monkeypatch.setenv("QUICKNODE_CHAIN_ID", "8453")
    assert any("quiknode.pro/QN" in u for u in rpc_urls(get_spec(8453)))
    assert not any("quiknode" in u for u in rpc_urls(get_spec(42161)))


def test_explicit_chain_env_beats_providers(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "ALC")
    monkeypatch.setenv("DEX_RPC_URL", "https://my-base-rpc")
    assert rpc_urls(get_spec(8453)) == ["https://my-base-rpc"]


def test_per_provider_chain_override(monkeypatch):
    monkeypatch.setenv("ALCHEMY_API_KEY", "ALC")
    monkeypatch.setenv("ALCHEMY_BASE_URL", "https://custom-alchemy/base")
    assert rpc_urls(get_spec(8453))[0] == "https://custom-alchemy/base"


def test_provider_status_reflects_keys(monkeypatch):
    assert rpc_providers.provider_status(rpc_providers.load_providers()) == {
        "alchemy": False, "infura": False, "quicknode": False,
    }
    monkeypatch.setenv("ALCHEMY_API_KEY", "ALC")
    monkeypatch.setenv("QUICKNODE_API_KEY", "QN")
    monkeypatch.setenv("QUICKNODE_ENDPOINT", "base-mainnet")
    assert rpc_providers.provider_status(rpc_providers.load_providers()) == {
        "alchemy": True, "infura": False, "quicknode": True,
    }


def test_unknown_chain_has_no_provider_urls():
    assert rpc_providers.alchemy_urls(999999, "ALC") == []
    assert rpc_providers.infura_urls(999999, "INF") == []
