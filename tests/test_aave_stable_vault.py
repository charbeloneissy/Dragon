import pytest

from src.dragon.aave_stable_vault import (
    StableVaultConfigError,
    load_stable_vaults_from_env,
    parse_stable_vault_config,
    validate_stable_vault,
)


def test_parse_stable_vault_multi_chain():
    config = parse_stable_vault_config(
        {
            "name": "dragon-stable",
            "vault_address": "0x1234567890abcdef1234567890abcdef12345678",
            "accounting_chain_id": 8453,
            "earning_chain_ids": [1, 42161],
            "supported_stablecoins": ["USDC", "USDT"],
            "strategy_names": ["AaveV3", "AaveV4"],
            "user_rate_apr_pct": "5.25",
            "allowlist_enabled": True,
            "metadata": {"allowlist": ["0x1111111111111111111111111111111111111111"]},
        }
    )
    assert config.accounting_chain_id == 8453
    assert config.earning_chain_ids == (1, 42161)
    assert config.supported_stablecoins == ("USDC", "USDT")
    assert str(config.user_rate_apr_pct) == "5.25"
    snapshot = validate_stable_vault(config)
    assert snapshot.healthy is True


def test_stable_vault_rejects_accounting_chain_overlap():
    with pytest.raises(StableVaultConfigError):
        parse_stable_vault_config(
            {
                "name": "bad",
                "vault_address": "0x1234567890abcdef1234567890abcdef12345678",
                "accounting_chain_id": 1,
                "earning_chain_ids": [1, 42161],
                "supported_stablecoins": ["USDC"],
                "strategy_names": ["AaveV3"],
            }
        )


def test_allowlist_requires_metadata():
    config = parse_stable_vault_config(
        {
            "name": "allowlisted",
            "vault_address": "0x1234567890abcdef1234567890abcdef12345678",
            "accounting_chain_id": 8453,
            "earning_chain_ids": [1],
            "supported_stablecoins": ["USDC"],
            "strategy_names": ["AaveV3"],
            "allowlist_enabled": True,
        }
    )
    snapshot = validate_stable_vault(config)
    assert snapshot.healthy is False
    assert "allowlist enabled" in snapshot.issues


def test_load_from_env(monkeypatch):
    monkeypatch.setenv(
        "AAVE_STABLE_VAULTS_JSON",
        '[{"name":"demo","vault_address":"0x1234567890abcdef1234567890abcdef12345678","accounting_chain_id":8453,"earning_chain_ids":[1],"supported_stablecoins":["USDC"],"strategy_names":["AaveV3"]}]',
    )
    configs = load_stable_vaults_from_env()
    assert len(configs) == 1
    assert configs[0].name == "demo"
