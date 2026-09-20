from src.dragon.universe import REQUESTED_UNIVERSE, requested_universe, universe_payload


def test_requested_universe_matches_unique_screenshot_selection():
    assert len(REQUESTED_UNIVERSE) == 48
    assert len({item.key for item in REQUESTED_UNIVERSE}) == 48
    assert {item.key for item in REQUESTED_UNIVERSE} >= {
        "ethereum", "base", "bnb", "solana", "tron", "osmosis", "abstract", "0g",
    }


def test_universe_can_be_filtered_by_key(monkeypatch):
    monkeypatch.setenv("UNIVERSE_CHAINS", "monad, 0g")
    assert [item.key for item in requested_universe()] == ["monad", "0g"]


def test_universe_payload_marks_active_runtime_entries(monkeypatch):
    monkeypatch.delenv("UNIVERSE_CHAINS", raising=False)
    rows = universe_payload({
        "ethereum": {"venues": ["Uniswap_V3", "Uniswap_V2"]},
        "solana": {"venues": ["Jupiter"]},
        "cosmos": {"venues": ["Osmosis"]},
    })
    by_key = {row["key"]: row for row in rows}
    assert by_key["ethereum"]["scan_ready"] is True
    assert by_key["solana"]["scan_ready"] is True
    assert by_key["osmosis"]["scan_ready"] is True
    assert by_key["monad"]["status"] == "watchlist"
    assert by_key["sahara"]["network_status"] == "testnet_only"


def test_evm_universe_entry_needs_two_venues(monkeypatch):
    monkeypatch.delenv("UNIVERSE_CHAINS", raising=False)
    rows = universe_payload({"unichain": {"venues": ["Uniswap_V3"]}})
    row = next(item for item in rows if item["key"] == "unichain")
    assert row["scan_ready"] is False
    assert row["status"] == "watchlist"
