import pytest

from src.dragon.helius_solana import HeliusConfig, HeliusError, HeliusSolanaClient


def test_helius_config_defaults():
    cfg = HeliusConfig(api_key="x")
    assert cfg.enabled
    assert "helius-rpc.com" in cfg.rpc_url


def test_priority_fee_requires_target():
    client = HeliusSolanaClient(HeliusConfig(api_key="x"))
    with pytest.raises(ValueError):
        import asyncio
        asyncio.run(client.get_priority_fee_estimate())


def test_priority_fee_rejects_two_targets():
    client = HeliusSolanaClient(HeliusConfig(api_key="x"))
    with pytest.raises(ValueError):
        import asyncio
        asyncio.run(
            client.get_priority_fee_estimate(
                account_keys=["11111111111111111111111111111111"],
                transaction="abc",
            )
        )


def test_missing_key_fails_cleanly():
    client = HeliusSolanaClient(HeliusConfig(api_key=""))
    with pytest.raises(HeliusError):
        import asyncio
        asyncio.run(client.get_slot())
