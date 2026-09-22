from dataclasses import dataclass

import pytest

from src.dragon.base_network import (
    BASE_MAINNET,
    BASE_SEPOLIA,
    BaseExecutionGuard,
    BaseNetworkAdapter,
    LiveExecutionBlocked,
    profile_for,
)
from src.dragon.chains import get_spec
from src.dragon.venues import venues_for


class FakeEth:
    def __init__(self, chain_id: int):
        self.chain_id = chain_id
        self.block_number = 123
        self.gas_price = 100


class FakeWeb3:
    def __init__(self, chain_id: int):
        self.eth = FakeEth(chain_id)


def test_profiles_have_explicit_execution_policy():
    assert profile_for(BASE_MAINNET).live_execution_allowed is True
    assert profile_for(BASE_SEPOLIA).live_execution_allowed is False


def test_profile_rejects_non_base():
    with pytest.raises(Exception):
        profile_for(1)


def test_adapter_rejects_rpc_chain_mismatch():
    with pytest.raises(Exception, match="RPC chain mismatch"):
        BaseNetworkAdapter(BASE_MAINNET, rpc_url="http://fake", w3=FakeWeb3(BASE_SEPOLIA))


def test_mainnet_adapter_accepts_mainnet_rpc():
    adapter = BaseNetworkAdapter(BASE_MAINNET, rpc_url="http://fake", w3=FakeWeb3(BASE_MAINNET))
    assert adapter.chain_id == BASE_MAINNET
    assert adapter.live_execution_allowed is True
    assert adapter.latest_block() == 123


def test_sepolia_adapter_is_read_only():
    adapter = BaseNetworkAdapter(BASE_SEPOLIA, rpc_url="http://fake", w3=FakeWeb3(BASE_SEPOLIA))
    assert adapter.chain_id == BASE_SEPOLIA
    assert adapter.live_execution_allowed is False
    assert adapter.venues == ()


def test_sepolia_live_execution_is_hard_blocked():
    adapter = BaseNetworkAdapter(BASE_SEPOLIA, rpc_url="http://fake", w3=FakeWeb3(BASE_SEPOLIA))
    with pytest.raises(LiveExecutionBlocked):
        adapter.guard.assert_live_execution_allowed()


def test_mainnet_venues_are_not_visible_on_sepolia():
    assert venues_for(BASE_SEPOLIA) == ()
    assert len(venues_for(BASE_MAINNET)) > 0


def test_chain_registry_rpc_defaults():
    assert get_spec(BASE_MAINNET).default_rpc == "https://mainnet.base.org"
    assert get_spec(BASE_SEPOLIA).default_rpc == "https://sepolia.base.org"


def test_guard_rejects_mainnet_venue_on_sepolia():
    adapter = BaseNetworkAdapter(BASE_SEPOLIA, rpc_url="http://fake", w3=FakeWeb3(BASE_SEPOLIA))
    venue = venues_for(BASE_MAINNET)[0]
    with pytest.raises(Exception, match="not registered"):
        adapter.guard.assert_venue(venue)
