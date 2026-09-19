from src.dragon.local_amm import V2PoolState, V3PoolState, optimize_unimodal

def test_v2_quote_preserves_invariant_direction():
    p=V2PoolState("A","B",1_000_000,1_000_000,30)
    out=p.quote("A",10_000)
    assert 0 < out < 10_000

def test_v3_token0_and_token1_quotes_positive():
    p=V3PoolState("A","B",1<<96,1_000_000_000,500)
    assert p.quote_without_crossing("A",1000)>0
    assert p.quote_without_crossing("B",1000)>0

def test_optimizer_finds_peak():
    x,p=optimize_unimodal(lambda n: -(n-50)*(n-50)+2500,1,100)
    assert x in range(45,56)
    assert p>=2490
