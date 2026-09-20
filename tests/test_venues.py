from src.dragon.venues import venue_names


def test_scroll_has_two_cross_exchange_venues():
    assert venue_names(534352) == ("SyncSwap_V2", "Uniswap_V3", "SushiSwap_V2")
