from dragon.models import Opportunity
from dragon.risk import approve

def make_opp(notional=25, edge=30): return Opportunity("BTCUSDT","A","B",100,101,100,edge,notional)
def test_risk_accepts_edge_and_notional(): assert approve(make_opp(),20,50)
def test_risk_rejects_large_notional(): assert not approve(make_opp(51),20,50)
def test_risk_rejects_insufficient_edge(): assert not approve(make_opp(edge=19),20,50)
