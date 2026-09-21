from src.dragon.chains import get_spec, rpc_urls_for


def test_base_mainnet_network_profile():
    spec = get_spec(8453)
    assert spec is not None
    assert spec.name == "base"
    assert spec.chain_id == 8453
    assert spec.native_symbol == "ETH"
    assert spec.explorer == "https://basescan.org"


def test_base_sepolia_network_profile():
    spec = get_spec(84532)
    assert spec is not None
    assert spec.name == "base-sepolia"
    assert spec.chain_id == 84532
    assert spec.native_symbol == "ETH"
    assert spec.explorer == "https://sepolia.basescan.org"
    assert rpc_urls_for(84532)[0] == "https://sepolia.base.org"


def test_base_sepolia_has_no_mainnet_dex_venues():
    from src.dragon.venues import venues_for
    assert venues_for(84532) == ()
