from decimal import Decimal

from src.dragon.hardening import CircuitBreaker, DynamicFee, LatencyMeter
from src.dragon.futures import FuturesOpportunity


def test_circuit_breaker_trips_and_recovers():
    cb = CircuitBreaker(failures=2, cooldown_seconds=60)
    assert cb.allow()
    cb.failure()
    count, until = cb.failure()
    assert count == 2
    assert until > 0
    assert not cb.allow()
    cb.success()
    assert cb.allow()


def test_latency_meter_reports_percentile():
    meter = LatencyMeter(size=10)
    for value in (1, 2, 3, 4, 10):
        meter.add(value)
    summary = meter.summary()
    assert summary["count"] == 5
    assert summary["max_ms"] == 10
    assert summary["p95_ms"] == 10


def test_dynamic_fee_reads_binance_taker_rate():
    class Client:
        def account(self):
            return {"commissionRates": {"taker": "0.0004"}}
    fee = DynamicFee(10)
    assert fee.refresh(Client()) == Decimal("4.0000")


def test_futures_basis_includes_funding_cost():
    opp = FuturesOpportunity(
        "BTCUSDT", Decimal("100"), Decimal("100"), Decimal("100.20"), Decimal("100.20"),
        Decimal("5"), Decimal("2"),
    )
    assert opp.cash_and_carry_bps() == Decimal("8")
