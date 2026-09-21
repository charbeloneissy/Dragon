from dragon.hash_utils import (
    opportunity_hash,
    sha1_base64,
    sha1_hex,
    sha256_base64,
    sha256_hex,
)


def test_sha1_base64_matches_bigquery_example():
    assert sha1_base64("Hello World") == "Ck1VqNd45QIvq3AZd8XYQLvEhtA="


def test_sha1_hex_is_deterministic():
    assert sha1_hex("Hello World") == "0a4d5556a35de39408beadc065df176102c7bd1c"


def test_sha256_hex_matches_bigquery_sha256():
    assert sha256_hex("Hello World") == (
        "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b"
        "f55b277d9ad9f146e"
    )


def test_sha256_base64_is_32_bytes():
    assert sha256_base64("Hello World") == (
        "pZGm1Av0IEBooRczbPe5jW2MZb8LzaMr"
        "9VsnfZrZ8UY="
    )


def test_opportunity_hash_is_stable():
    args = dict(
        chain_id=8453,
        buy_source="Uniswap",
        sell_source="Aerodrome",
        base_token="0xbase",
        quote_token="0xquote",
        quote_amount=1000000,
        quote_version="v1",
    )
    assert opportunity_hash(**args) == opportunity_hash(**args)
    assert len(opportunity_hash(**args)) == 64
