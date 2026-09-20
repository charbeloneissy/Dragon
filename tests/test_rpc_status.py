import time

from dragon.dex_direct import DirectDexAdapter


def test_rpc_host_masks_key():
    url = "https://base-mainnet.g.alchemy.com/v2/abcdefgh12345678"
    assert DirectDexAdapter._rpc_host(url) == "base-mainnet.g.alchemy.com"
    assert "abcdefgh" not in DirectDexAdapter._rpc_host(url)


def test_rpc_host_handles_empty_and_bad_values():
    assert DirectDexAdapter._rpc_host("") == "none"


def _bare_adapter(urls):
    adapter = DirectDexAdapter.__new__(DirectDexAdapter)
    adapter._rpc_urls = urls
    adapter._keyed_endpoints = [DirectDexAdapter._is_keyed_rpc_endpoint(u) for u in urls]
    adapter._rpc_selftest = ["ok" for _ in urls]
    adapter._rpc_success = [3 for _ in urls]
    adapter._rpc_errors = [1 for _ in urls]
    adapter._rpc_cooldown_until = [0.0 for _ in urls]
    adapter._rpc_index = 0
    adapter.rpc_url = urls[0]
    return adapter


def test_rpc_status_reports_hosts_without_secrets():
    adapter = _bare_adapter([
        "https://base-mainnet.g.alchemy.com/v2/abcdefgh12345678",
        "https://mainnet.base.org/",
    ])
    status = adapter.rpc_status()
    assert status["active_host"] == "base-mainnet.g.alchemy.com"
    assert [e["host"] for e in status["endpoints"]] == ["base-mainnet.g.alchemy.com", "mainnet.base.org"]
    assert status["endpoints"][0]["keyed"] is True
    assert status["endpoints"][1]["keyed"] is False
    assert "abcdefgh" not in str(status)


def test_rpc_status_marks_cooling_down():
    adapter = _bare_adapter(["https://mainnet.base.org/"])
    adapter._rpc_cooldown_until = [time.monotonic() + 60]
    assert adapter.rpc_status()["endpoints"][0]["cooling_down"] is True


def test_rpc_status_survives_short_selftest_list():
    adapter = _bare_adapter(["https://mainnet.base.org/"])
    adapter._rpc_selftest = []
    assert adapter.rpc_status()["endpoints"][0]["selftest"] == "unknown"
