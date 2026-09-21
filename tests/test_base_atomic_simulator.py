from decimal import Decimal

from src.dragon.base_atomic_simulator import BaseAtomicSimulator


def test_simulator_requires_deployed_executor(monkeypatch):
    monkeypatch.delenv("DRAGON_ATOMIC_EXECUTOR", raising=False)
    sim = BaseAtomicSimulator(min_profit=Decimal("0.005"))
    result = sim.simulate(
        adapter=object(),
        opportunity=type("O", (), {"chain_id": 8453})(),
        taker="0x0000000000000000000000000000000000000001",
    )
    assert not result.passed
    assert "not configured" in result.reason


def test_profit_floor_is_hard():
    try:
        BaseAtomicSimulator(min_profit=Decimal("0.004"))
    except ValueError:
        return
    raise AssertionError("profit floor must not be configurable below $0.005")
