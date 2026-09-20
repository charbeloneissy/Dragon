from decimal import Decimal
from time import monotonic

from src.dragon.opportunity_engine import (
    ExecutionBudget,
    OpportunityEngine,
    OpportunityStage,
    ProfitProof,
)


def proof(net="0.010"):
    return ProfitProof(
        gross_profit=Decimal("0.020"),
        dex_fees=Decimal("0.002"),
        gas_cost=Decimal("0.003"),
        flash_loan_fee=Decimal("0.001"),
        slippage_cost=Decimal("0.001"),
        mev_cost=Decimal("0.001"),
        risk_buffer=Decimal("0.002"),
    )


def test_profit_proof_is_net_of_all_costs():
    assert proof().expected_net == Decimal("0.010")


def test_stale_quote_is_expired():
    engine = OpportunityEngine(min_net_profit=Decimal("0.005"))
    result = engine.assess(
        opportunity_id="x",
        chain="Base",
        pair="WETH/USDC",
        proof=proof(),
        quote_age_ms=1501,
        state_fresh=True,
        simulation_passed=True,
        mev_available=True,
    )
    assert result.stage == OpportunityStage.EXPIRED
    assert "quote_stale" in result.reasons


def test_low_survival_is_rejected():
    engine = OpportunityEngine(
        min_net_profit=Decimal("0.005"),
        min_survival_probability=Decimal("0.70"),
    )
    result = engine.assess(
        opportunity_id="x",
        chain="Base",
        pair="WETH/USDC",
        proof=proof(),
        quote_age_ms=20,
        state_fresh=True,
        simulation_passed=True,
        mev_available=True,
        historical_survival=Decimal("0.50"),
    )
    assert result.stage == OpportunityStage.REJECTED


def test_valid_opportunity_reaches_execution_gate():
    engine = OpportunityEngine(min_net_profit=Decimal("0.005"))
    result = engine.assess(
        opportunity_id="x",
        chain="Base",
        pair="WETH/USDC",
        proof=proof(),
        quote_age_ms=20,
        state_fresh=True,
        simulation_passed=True,
        mev_available=True,
        historical_survival=Decimal("0.90"),
        execution_budget=ExecutionBudget(monotonic(), 1000),
    )
    assert result.stage == OpportunityStage.EXECUTION_APPROVED


def test_realized_pnl_is_separate_from_expected():
    actual = OpportunityEngine.realized_net(
        proceeds=Decimal("1.020"),
        principal=Decimal("1.000"),
        gas_cost=Decimal("0.004"),
        dex_fees=Decimal("0.002"),
        flash_loan_fee=Decimal("0.001"),
    )
    assert actual == Decimal("0.013")
