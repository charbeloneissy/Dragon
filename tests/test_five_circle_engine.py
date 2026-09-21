from decimal import Decimal
from types import SimpleNamespace

from src.dragon.five_circle_engine import FiveCircleEngine


def opp(net, gross=0.02, gas=0.001, chain_id=8453):
    return SimpleNamespace(
        chain_id=chain_id,
        net_profit_quote=Decimal(str(net)),
        gross_profit_quote=Decimal(str(gross)),
        gas_cost_quote=Decimal(str(gas)),
    )


def test_challenge_rejects_candidate_that_only_looks_profitable():
    engine = FiveCircleEngine(
        min_profit=Decimal("0.005"),
        gas_stress_bps=2000,
        execution_stress_bps=100,
    )
    result = engine.run(
        [opp("0.0052", gross="0.0056", gas="0.002")],
        rotation=1,
        chain_id=8453,
        block_number=100,
    )
    assert [d.circle for d in result.decisions] == ["DISCOVER", "OPTIMIZE", "CHALLENGE"]
    assert result.selected is None


def test_prove_requires_explicit_simulation_result():
    engine = FiveCircleEngine()
    candidate = opp("0.02", gross="0.03", gas="0.001")
    result = engine.run([candidate], rotation=2, chain_id=8453, block_number=101)
    assert result.decisions[-1].circle == "PROVE"
    assert not result.executable

    proven = engine.run(
        [candidate],
        rotation=3,
        chain_id=8453,
        block_number=102,
        simulation_passed=True,
    )
    assert proven.executable
    assert proven.selected is candidate


def test_minimum_profit_is_hard_floor():
    try:
        FiveCircleEngine(min_profit=Decimal("0.004"))
    except ValueError:
        return
    raise AssertionError("minimum profit floor was not enforced")
