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


def test_build_deposit_and_withdraw_transactions():
    from src.dragon.aave_stable_vault import (
        build_deposit_tx,
        build_execute_withdrawal_tx,
        build_request_withdrawal_tx,
    )

    vault = "0x1234567890abcdef1234567890abcdef12345678"
    user = "0x1111111111111111111111111111111111111111"
    usdc = "0x2222222222222222222222222222222222222222"

    deposit = build_deposit_tx(
        chain_id=8453,
        vault=vault,
        user=user,
        asset=usdc,
        amount=1000000,
        policy_data="0x",
    )
    request = build_request_withdrawal_tx(
        chain_id=8453,
        vault=vault,
        user=user,
        requested_amount_ray=10**27,
        policy_data="0x",
    )
    execute = build_execute_withdrawal_tx(
        chain_id=8453,
        vault=vault,
        user=user,
        asset_out=usdc,
        min_amount_out=990000,
        iou_amount_ray=10**27,
        policy_data="0x",
    )

    for tx in (deposit, request, execute):
        assert tx["to"] == vault
        assert tx["value"] == "0"
        assert tx["chainId"] == 8453
        assert tx["data"].startswith("0x")
        assert len(tx["data"]) > 10

    assert deposit["data"].startswith("0x")
    assert request["data"].startswith("0x")
    assert execute["data"].startswith("0x")
