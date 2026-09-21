from decimal import Decimal

from dragon.aave_v3_liquidity import AaveV3LiquiditySnapshot


def test_snapshot_exposes_live_liquidity_and_fee():
    snap = AaveV3LiquiditySnapshot(
        chain_id=8453,
        pool="0x0000000000000000000000000000000000000001",
        asset="0x0000000000000000000000000000000000000002",
        available_units=123456,
        premium_bps=Decimal("5"),
        block_number=100,
    )
    assert snap.available == Decimal("123456")
    assert snap.premium_bps == Decimal("5")


def test_snapshot_never_rounds_liquidity():
    snap = AaveV3LiquiditySnapshot(
        chain_id=1,
        pool="0x0000000000000000000000000000000000000001",
        asset="0x0000000000000000000000000000000000000002",
        available_units=999999999999,
        premium_bps=Decimal("5"),
        block_number=200,
    )
    assert snap.available_units == 999999999999
