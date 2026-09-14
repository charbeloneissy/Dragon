from dragon.arbitrage import edge_bps, find_opportunity
from dragon.models import Quote

def test_edge_bps(): assert round(edge_bps(100, 101), 4) == 100.0

def test_opportunity_accounts_for_round_trip_costs():
    opp = find_opportunity("BTCUSDT", "A", Quote("BTCUSDT",99.9,100), "B", Quote("BTCUSDT",101,101.1), 10, 5, 25)
    assert opp is not None
    assert round(opp.net_edge_bps, 4) == 70.0

def test_no_opportunity_when_costs_erase_edge():
    assert find_opportunity("BTCUSDT", "A", Quote("BTCUSDT",99.9,100), "B", Quote("BTCUSDT",100.2,100.3), 10, 5, 25) is None
