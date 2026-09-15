from decimal import Decimal

from src.dragon.futures import evaluate_basis


def test_basis_accepts_positive_cash_and_carry_edge():
    rows = [{"symbol": "BTCUSDT", "spotBid": "100", "spotAsk": "100.01", "futuresBid": "100.20", "futuresAsk": "100.21"}]
    result = evaluate_basis(rows, Decimal("5"), Decimal("8"), Decimal("0"))
    assert result and result[0][0] == "BTCUSDT"
    assert result[0][2] == "SPOT_LONG_FUT_SHORT"


def test_basis_rejects_negative_edge_in_both_directions():
    rows = [{"symbol": "BTCUSDT", "spotBid": "100", "spotAsk": "100.01", "futuresBid": "100.00", "futuresAsk": "100.01"}]
    assert evaluate_basis(rows, Decimal("5"), Decimal("0.1"), Decimal("0")) == []


def test_basis_detects_reverse_futures_long_spot_short():
    rows = [{"symbol": "BTCUSDT", "spotBid": "100.20", "spotAsk": "100.21", "futuresBid": "99.99", "futuresAsk": "100.00"}]
    result = evaluate_basis(rows, Decimal("5"), Decimal("10"), Decimal("0"))
    assert result and result[0][2] == "FUT_LONG_SPOT_SHORT"
