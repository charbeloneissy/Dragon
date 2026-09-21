from decimal import Decimal
from types import SimpleNamespace
from src.dragon.base_proof_engine import BaseProofEngine

def test_rejects_non_base():
    r=BaseProofEngine().prove(adapter=object(), opportunity=SimpleNamespace(chain_id=1), taker="0x000000000000000000000000000000000000dEaD")
    assert not r.passed

def test_floor():
    try: BaseProofEngine(min_profit=Decimal("0.004"))
    except ValueError: return
    raise AssertionError("floor not enforced")
