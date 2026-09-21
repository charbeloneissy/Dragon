from dragon.hash_utils import sha1_base64, sha1_hex


def test_sha1_base64_matches_bigquery_example():
    assert sha1_base64("Hello World") == "Ck1VqNd45QIvq3AZd8XYQLvEhtA="


def test_sha1_hex_is_deterministic():
    assert sha1_hex("Hello World") == "0a4d5556a35de39408beadc065df176102c7bd1c"
