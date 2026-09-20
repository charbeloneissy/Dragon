"""Dragon Opportunity Engine v2.

Pure decision/state primitives for the observe -> prove -> execute pipeline.
No transaction is submitted from this module. Execution authority remains
outside the dashboard and requires an explicit risk/execution gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from time import monotonic
from typing import Mapping


class OpportunityStage(str, Enum):
    DETECTED = "detected"
    STATE_VALID = "state_valid"
    QUOTE_VALID = "quote_valid"
    COST_VALID = "cost_valid"
    MEV_VALID = "mev_valid"
    SIMULATION_VALID = "simulation_valid"
    REQUOTED = "requote_valid"
    EXECUTION_APPROVED = "execution_approved"
    CONFIRMED = "confirmed"
    REALIZED = "realized"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVERTED = "reverted"


@dataclass(frozen=True)
class ProfitProof:
    gross_profit: Decimal
    dex_fees: Decimal = Decimal("0")
    gas_cost: Decimal = Decimal("0")
    flash_loan_fee: Decimal = Decimal("0")
    slippage_cost: Decimal = Decimal("0")
    mev_cost: Decimal = Decimal("0")
    risk_buffer: Decimal = Decimal("0")

    @property
    def expected_net(self) -> Decimal:
        return (
            self.gross_profit
            - self.dex_fees
            - self.gas_cost
            - self.flash_loan_fee
            - self.slippage_cost
            - self.mev_cost
            - self.risk_buffer
        )


@dataclass(frozen=True)
class ExecutionBudget:
    created_monotonic: float
    deadline_ms: int
    observed_latency_ms: float = 0.0

    @property
    def age_ms(self) -> float:
        return max(0.0, (monotonic() - self.created_monotonic) * 1000.0)

    @property
    def remaining_ms(self) -> float:
        return self.deadline_ms - self.age_ms - self.observed_latency_ms

    @property
    def viable(self) -> bool:
        return self.remaining_ms > 0


@dataclass
class OpportunityAssessment:
    opportunity_id: str
    chain: str
    pair: str
    stage: OpportunityStage = OpportunityStage.DETECTED
    proof: ProfitProof | None = None
    survival_probability: Decimal = Decimal("0")
    reasons: list[str] = field(default_factory=list)
    metadata: dict[str, object] = field(default_factory=dict)

    def reject(self, reason: str, stage: OpportunityStage = OpportunityStage.REJECTED) -> "OpportunityAssessment":
        self.stage = stage
        self.reasons.append(reason)
        self.survival_probability = Decimal("0")
        return self


class OpportunityEngine:
    """Deterministic pre-execution gate and opportunity-quality scorer."""

    def __init__(
        self,
        *,
        min_net_profit: Decimal,
        max_quote_age_ms: int = 1000,
        min_survival_probability: Decimal = Decimal("0.60"),
        require_mev_protection: bool = True,
    ):
        self.min_net_profit = Decimal(min_net_profit)
        self.max_quote_age_ms = int(max_quote_age_ms)
        self.min_survival_probability = Decimal(min_survival_probability)
        self.require_mev_protection = require_mev_protection

    def assess(
        self,
        *,
        opportunity_id: str,
        chain: str,
        pair: str,
        proof: ProfitProof,
        quote_age_ms: float,
        state_fresh: bool,
        simulation_passed: bool,
        mev_available: bool,
        execution_budget: ExecutionBudget | None = None,
        historical_survival: Decimal | None = None,
    ) -> OpportunityAssessment:
        a = OpportunityAssessment(
            opportunity_id=opportunity_id,
            chain=chain,
            pair=pair,
            proof=proof,
        )

        if not state_fresh:
            return a.reject("state_uncertain")
        a.stage = OpportunityStage.STATE_VALID

        if quote_age_ms > self.max_quote_age_ms:
            return a.reject("quote_stale", OpportunityStage.EXPIRED)
        a.stage = OpportunityStage.QUOTE_VALID

        if proof.expected_net < self.min_net_profit:
            return a.reject("net_profit_below_threshold")
        a.stage = OpportunityStage.COST_VALID

        if self.require_mev_protection and not mev_available:
            return a.reject("mev_protection_unavailable")
        a.stage = OpportunityStage.MEV_VALID

        if execution_budget is not None and not execution_budget.viable:
            return a.reject("execution_budget_exhausted", OpportunityStage.EXPIRED)

        if not simulation_passed:
            return a.reject("simulation_failed")
        a.stage = OpportunityStage.SIMULATION_VALID

        if historical_survival is None:
            survival = Decimal("0.50")
        else:
            survival = min(Decimal("1"), max(Decimal("0"), Decimal(historical_survival)))
        a.survival_probability = survival
        a.metadata["expected_net_profit"] = str(proof.expected_net)
        a.metadata["quote_age_ms"] = quote_age_ms

        if survival < self.min_survival_probability:
            return a.reject("survival_probability_below_threshold")

        a.stage = OpportunityStage.REQUOTED
        a.stage = OpportunityStage.EXECUTION_APPROVED
        return a

    @staticmethod
    def realized_net(
        *,
        proceeds: Decimal,
        principal: Decimal,
        gas_cost: Decimal,
        dex_fees: Decimal = Decimal("0"),
        flash_loan_fee: Decimal = Decimal("0"),
        other_costs: Decimal = Decimal("0"),
    ) -> Decimal:
        return (
            Decimal(proceeds)
            - Decimal(principal)
            - Decimal(gas_cost)
            - Decimal(dex_fees)
            - Decimal(flash_loan_fee)
            - Decimal(other_costs)
        )

    @staticmethod
    def priority_score(
        expected_net: Decimal,
        survival_probability: Decimal,
        infrastructure_cost: Decimal = Decimal("0"),
    ) -> Decimal:
        return (
            Decimal(expected_net) * Decimal(survival_probability)
            - Decimal(infrastructure_cost)
        )
