import pytest

from dragon.dex_direct import DirectDexAdapter


PUBLIC = "https://mainnet.base.org/"
KEYED = "https://base-mainnet.g.alchemy.com/v2/abcdefgh12345678"
DEMO = "https://abstract-mainnet.g.alchemy.com/v2/docs-demo"


@pytest.mark.parametrize(
    "url,expected",
    [
        (PUBLIC, False),
        (KEYED, True),
        (DEMO, False),
        ("https://base-mainnet.g.alchemy.com/v2/YOUR_KEY", False),
        ("https://mainnet.base.org", False),
    ],
)
def test_keyed_endpoint_detection(url, expected):
    assert DirectDexAdapter._is_keyed_rpc_endpoint(url) is expected


def test_keyed_endpoint_wins_rotation_over_public(monkeypatch):
    monkeypatch.setenv("DEX_RPC_URL", PUBLIC)
    monkeypatch.setenv("DEX_RPC_URLS", KEYED)
    monkeypatch.setenv("DEX_RPC_TIMEOUT_SECONDS", "0.4")
    adapter = DirectDexAdapter.__new__(DirectDexAdapter)
    urls = [PUBLIC, KEYED]
    adapter._keyed_endpoints = [DirectDexAdapter._is_keyed_rpc_endpoint(u) for u in urls]
    adapter._has_keyed_endpoint = any(adapter._keyed_endpoints)
    assert adapter._keyed_endpoints == [False, True]

