from decimal import Decimal

from dragon.triangles import Triangle, evaluate_triangle


def test_triangle_uses_direct_and_inverse_conversions():
    t = Triangle(("ETHUSDT", "ETHBTC", "BTCUSDT"), ("USDT", "ETH", "BTC"))
    books = {
        "ETHUSDT": {"bids": [[100, 5]], "asks": [[101, 5]], "ts": 1},
        "ETHBTC": {"bids": [[0.0102, 5]], "asks": [[0.0103, 5]], "ts": 1},
        "BTCUSDT": {"bids": [[10000, 5]], "asks": [[10001, 5]], "ts": 1},
    }
    result = evaluate_triangle(t, books, fee_bps=1, slippage_bps=0, notional_usdt=10)
    assert result is not None
    net_bps, gross_bps, path, first, second = result
    assert path == t.symbols
    assert first == "ETH" and second == "BTC"
    assert gross_bps > Decimal("0")
    assert net_bps < gross_bps


def test_triangle_returns_none_without_complete_book():
    t = Triangle(("ETHUSDT", "ETHBTC", "BTCUSDT"), ("USDT", "ETH", "BTC"))
    books = {"ETHUSDT": {"bids": [[100, 5]], "asks": [[101, 5]], "ts": 1}}
    assert evaluate_triangle(t, books, 1, 0) is None
