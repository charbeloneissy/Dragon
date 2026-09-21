from decimal import Decimal

from src.dragon.aave_protocol_intelligence import (
    OracleSnapshot,
    compare_reference,
    is_price_feed_candidate,
)


def test_oracle_gate_accepts_reference_inside_limit():
    snapshot = OracleSnapshot(
        chain_id=1,
        asset="0xToken",
        oracle="0xOracle",
        source="0xSource",
        price=Decimal("1.00"),
        oracle_decimals=8,
        expected_decimals=8,
        valid=True,
        is_capped=True,
        snapshot_ratio=Decimal("1.05"),
        snapshot_timestamp=123,
        max_yearly_growth_pct=Decimal("5"),
        adapter_description="Capped USDC / USD",
    )
    result = compare_reference(
        snapshot,
        reference_price=Decimal("1.001"),
        max_deviation_bps=Decimal("25"),
        require_oracle=True,
    )
    assert result.allowed is True
    assert result.reason == "oracle_ok"
    assert result.deviation_bps is not None


def test_oracle_gate_rejects_large_deviation():
    snapshot = OracleSnapshot(
        chain_id=1,
        asset="0xToken",
        oracle="0xOracle",
        source="0xSource",
        price=Decimal("100"),
        oracle_decimals=8,
        expected_decimals=8,
        valid=True,
    )
    result = compare_reference(
        snapshot,
        reference_price=Decimal("90"),
        max_deviation_bps=Decimal("500"),
        require_oracle=True,
    )
    assert result.allowed is False
    assert result.reason == "oracle_deviation_too_large"


def test_missing_oracle_can_remain_non_blocking_by_policy():
    result = compare_reference(
        None,
        reference_price=Decimal("1"),
        max_deviation_bps=Decimal("500"),
        require_oracle=False,
    )
    assert result.allowed is True
    assert result.reason == "oracle_not_configured"


def test_price_feed_candidate_detection():
    assert is_price_feed_candidate("Capped wstETH / ETH / USD") is True
    assert is_price_feed_candidate("random adapter") is False
