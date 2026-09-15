from decimal import Decimal

from src.dragon.futures import evaluate_basis


def test_basis_requires_net_edge_after_two_fees():
    rows = [{"symbol": "BTCUSDT", "spotBid": "100", "spotAsk": "100.01", "futuresBid": "100.20", "futuresAsk": "100.21"}]
    result = evaluate_basis(rows, Decimal("5"), Decimal("8"))
    assert result and result[0][0] == "BTCUSDT"


def test_basis_rejects_negative_edge():
    rows = [{"symbol": "BTCUSDT", "spotBid": "100", "spotAsk": "100.01", "futuresBid": "100.00", "futuresAsk": "100.01"}]
    assert evaluate_basis(rows, Decimal("5"), Decimal("0.1")) == []
