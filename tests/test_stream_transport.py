from src.dragon.stream_transport import _combined_ws_url, _depth_payload


def test_combined_partial_depth_payload_uses_stream_symbol():
    msg = {
        "stream": "btcusdt@depth20@100ms",
        "data": {
            "lastUpdateId": 123,
            "bids": [["100.0", "2.0"]],
            "asks": [["101.0", "3.0"]],
        },
    }
    symbol, book = _depth_payload(msg, 20)
    assert symbol == "BTCUSDT"
    assert book["bids"] == [("100.0", "2.0")]
    assert book["asks"] == [("101.0", "3.0")]


def test_diff_depth_payload_still_works():
    msg = {
        "e": "depthUpdate",
        "s": "ETHUSDT",
        "b": [["2000.0", "1.0"]],
        "a": [["2001.0", "1.5"]],
    }
    symbol, book = _depth_payload(msg, 20)
    assert symbol == "ETHUSDT"
    assert book["bids"] == [("2000.0", "1.0")]
    assert book["asks"] == [("2001.0", "1.5")]


def test_combined_ws_url():
    assert _combined_ws_url("wss://stream.binance.com:9443/ws") == "wss://stream.binance.com:9443/stream"
    assert _combined_ws_url("wss://stream.binance.com:9443/stream") == "wss://stream.binance.com:9443/stream"
