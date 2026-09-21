from dragon.aave_markets import AAVE_V3_DEPLOYMENTS, get_aave_v3_deployment

def test_supported_markets_have_distinct_pool_deployments():
    assert get_aave_v3_deployment(1).pool == "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2"
    assert get_aave_v3_deployment(8453).pool == "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5"
    assert len({deployment.pool.lower() for deployment in AAVE_V3_DEPLOYMENTS.values()}) == 2

def test_unknown_chain_is_rejected():
    try:
        get_aave_v3_deployment(999999)
    except ValueError as exc:
        assert "unsupported Aave V3 market chain" in str(exc)
    else:
        raise AssertionError("unknown Aave chain should be rejected")


def test_market_discovery_requires_rpc():
    from dragon.aave_markets import AaveMarketDiscovery
    try:
        AaveMarketDiscovery({}).discover_markets([1, 8453])
    except RuntimeError as exc:
        assert "missing RPC URL for Aave chain 1" in str(exc)
    else:
        raise AssertionError("expected missing RPC validation")
