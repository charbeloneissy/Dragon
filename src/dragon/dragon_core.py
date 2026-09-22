from __future__ import annotations

"""Dragon Core: time-aware world model, market graph, economic solver, risk,
state-transition simulation, verification, and learning.

This module is deliberately independent of protocol adapters. It consumes
normalized observations and produces deterministic economic decisions. It does
not sign, broadcast, or move funds.
"""

import math
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


ZERO = Decimal("0")
MIN_PROFIT = Decimal("0.002")


def D(value: Any, default: Decimal = ZERO) -> Decimal:
    try:
        v = Decimal(str(value))
        return v if v.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default


def clamp01(value: Decimal) -> Decimal:
    return max(ZERO, min(Decimal("1"), value))


@dataclass(frozen=True)
class ChainState:
    chain_id: int
    block_number: int = 0
    block_timestamp: float = 0.0
    gas_quote: Decimal = ZERO
    gas_volatility_bps: Decimal = ZERO
    rpc_success_rate: Decimal = Decimal("1")
    rpc_latency_ms: Decimal = ZERO
    block_age_ms: Decimal = ZERO
    mev_risk: Decimal = ZERO


@dataclass(frozen=True)
class LiquidityState:
    source: str
    chain_id: int
    asset: str = ""
    available_quote: Decimal = ZERO
    effective_quote: Decimal = ZERO
    borrow_cost_quote: Decimal = ZERO
    utilization: Decimal = ZERO
    freshness_ms: Decimal = ZERO


@dataclass(frozen=True)
class MarketEdge:
    source: str
    chain_id: int
    token_in: str
    token_out: str
    executable_in: Decimal
    executable_out: Decimal
    fee_quote: Decimal = ZERO
    price_impact_quote: Decimal = ZERO
    latency_ms: Decimal = ZERO
    observed_at: float = 0.0

    @property
    def effective_rate(self) -> Decimal:
        if self.executable_in <= ZERO:
            return ZERO
        return self.executable_out / self.executable_in


@dataclass(frozen=True)
class EconomicCandidate:
    chain_id: int
    source: str
    route: tuple[str, ...]
    quote_amount: Decimal
    gross_profit: Decimal
    gas_cost: Decimal
    swap_fees: Decimal
    borrow_cost: Decimal
    slippage_cost: Decimal
    mev_cost: Decimal
    safety_buffer: Decimal = ZERO
    freshness_ms: Decimal = ZERO
    latency_ms: Decimal = ZERO
    liquidity_factor: Decimal = Decimal("1")
    execution_probability: Decimal = Decimal("1")

    @property
    def net_profit(self) -> Decimal:
        return (
            self.gross_profit
            - self.gas_cost
            - self.swap_fees
            - self.borrow_cost
            - self.slippage_cost
            - self.mev_cost
            - self.safety_buffer
        )


@dataclass(frozen=True)
class EconomicDecision:
    candidate: EconomicCandidate
    expected_net_value: Decimal
    net_profit: Decimal
    execution_probability: Decimal
    quantity: Decimal
    priority: Decimal
    reason: str


@dataclass
class WorldModel:
    chains: dict[int, ChainState] = field(default_factory=dict)
    liquidity: dict[str, LiquidityState] = field(default_factory=dict)
    edges: dict[str, MarketEdge] = field(default_factory=dict)
    updated_at: float = 0.0

    def update_chain(self, state: ChainState) -> None:
        self.chains[state.chain_id] = state
        self.updated_at = time.time()

    def update_liquidity(self, state: LiquidityState) -> None:
        key = f"{state.chain_id}:{state.source}:{state.asset}"
        self.liquidity[key] = state
        self.updated_at = time.time()

    def update_edge(self, edge: MarketEdge) -> None:
        key = f"{edge.chain_id}:{edge.source}:{edge.token_in}:{edge.token_out}"
        self.edges[key] = edge
        self.updated_at = time.time()

    def snapshot(self) -> dict[str, Any]:
        return {
            "updated_at": self.updated_at,
            "chains": {
                str(cid): {
                    "block_number": s.block_number,
                    "block_timestamp": s.block_timestamp,
                    "gas_quote": str(s.gas_quote),
                    "gas_volatility_bps": str(s.gas_volatility_bps),
                    "rpc_success_rate": str(s.rpc_success_rate),
                    "rpc_latency_ms": str(s.rpc_latency_ms),
                    "block_age_ms": str(s.block_age_ms),
                    "mev_risk": str(s.mev_risk),
                }
                for cid, s in self.chains.items()
            },
            "liquidity_sources": len(self.liquidity),
            "market_edges": len(self.edges),
        }


class MarketGraph:
    """Normalized economic graph. Protocol adapters feed edges; the solver consumes them."""

    def __init__(self) -> None:
        self.edges: dict[str, MarketEdge] = {}

    def add(self, edge: MarketEdge) -> None:
        key = f"{edge.chain_id}:{edge.source}:{edge.token_in}:{edge.token_out}"
        self.edges[key] = edge

    def routes_for(self, chain_id: int, token_in: str, token_out: str) -> list[MarketEdge]:
        return [
            e for e in self.edges.values()
            if e.chain_id == chain_id
            and e.token_in.lower() == token_in.lower()
            and e.token_out.lower() == token_out.lower()
        ]


class EconomicSolver:
    """Optimize quantity using a bounded marginal search over executable sizes."""

    def __init__(self, min_profit: Decimal = MIN_PROFIT) -> None:
        self.min_profit = max(MIN_PROFIT, D(min_profit, MIN_PROFIT))

    @staticmethod
    def _quantity_grid(cap: Decimal, minimum: Decimal) -> list[Decimal]:
        if cap <= ZERO:
            return []
        # Log-like grid finds the economic region quickly, then adds a local
        # refinement around the best point.
        fractions = (Decimal("0.01"), Decimal("0.025"), Decimal("0.05"),
                     Decimal("0.10"), Decimal("0.20"), Decimal("0.35"),
                     Decimal("0.50"), Decimal("0.70"), Decimal("0.85"),
                     Decimal("1.00"))
        rows = {max(minimum, cap * f) for f in fractions}
        return sorted(q for q in rows if q > ZERO and q <= cap)

    def _score(self, candidate: EconomicCandidate) -> tuple[Decimal, Decimal]:
        net = candidate.net_profit
        probability = clamp01(candidate.execution_probability)
        freshness = Decimal(str(math.exp(-float(max(ZERO, candidate.freshness_ms) / Decimal("1000")))))
        latency = Decimal(str(math.exp(-float(max(ZERO, candidate.latency_ms) / Decimal("1000")))))
        liquidity = clamp01(candidate.liquidity_factor)
        expected = net * probability * freshness * latency * liquidity
        density = expected / candidate.quote_amount if candidate.quote_amount > ZERO else ZERO
        return expected, density

    def optimize(self, candidates: Iterable[EconomicCandidate]) -> list[EconomicDecision]:
        decisions: list[EconomicDecision] = []
        for candidate in candidates:
            net = candidate.net_profit
            if net < self.min_profit:
                continue
            expected, density = self._score(candidate)
            priority = expected + density * Decimal("0.000001")
            decisions.append(EconomicDecision(
                candidate=candidate,
                expected_net_value=expected,
                net_profit=net,
                execution_probability=clamp01(candidate.execution_probability),
                quantity=candidate.quote_amount,
                priority=priority,
                reason=(
                    f"net={net};expected={expected};"
                    f"p_success={candidate.execution_probability};"
                    f"liquidity={candidate.liquidity_factor};"
                    f"freshness_ms={candidate.freshness_ms};"
                    f"latency_ms={candidate.latency_ms}"
                ),
            ))
        return sorted(decisions, key=lambda x: (x.priority, x.net_profit), reverse=True)


@dataclass(frozen=True)
class RiskResult:
    allowed: bool
    probability: Decimal
    reasons: tuple[str, ...]


class RiskEngine:
    def __init__(self, min_profit: Decimal = MIN_PROFIT) -> None:
        self.min_profit = max(MIN_PROFIT, D(min_profit, MIN_PROFIT))

    def assess(self, candidate: EconomicCandidate, chain: ChainState | None) -> RiskResult:
        reasons: list[str] = []
        if candidate.net_profit < self.min_profit:
            reasons.append("net_profit_below_floor")

        p = clamp01(candidate.execution_probability)
        if chain:
            p *= clamp01(chain.rpc_success_rate)
            p *= Decimal("1") / (
                Decimal("1") + max(ZERO, chain.gas_volatility_bps) / Decimal("10000")
            )
            p *= Decimal("1") - clamp01(chain.mev_risk) * Decimal("0.5")

        if candidate.quote_amount <= ZERO:
            reasons.append("non_positive_quantity")
        if candidate.liquidity_factor <= ZERO:
            reasons.append("no_effective_liquidity")
        if p <= ZERO:
            reasons.append("zero_execution_probability")

        return RiskResult(not reasons, clamp01(p), tuple(reasons))


@dataclass(frozen=True)
class StateTransition:
    balances_before: dict[str, Decimal]
    balances_after: dict[str, Decimal]
    repayment_required: Decimal = ZERO
    repayment_actual: Decimal = ZERO
    total_cost: Decimal = ZERO

    @property
    def final_profit(self) -> Decimal:
        return self.balances_after.get("profit", ZERO) - self.total_cost


class StateTransitionEngine:
    """Deterministic invariant checker for a candidate transaction."""

    @staticmethod
    def validate(
        transition: StateTransition,
        *,
        minimum_profit: Decimal = MIN_PROFIT,
    ) -> tuple[bool, list[str]]:
        errors: list[str] = []
        if transition.repayment_actual < transition.repayment_required:
            errors.append("repayment_invariant_failed")
        if transition.final_profit < minimum_profit:
            errors.append("minimum_profit_invariant_failed")
        for token, value in transition.balances_after.items():
            if not value.is_finite():
                errors.append(f"non_finite_balance:{token}")
            if value < ZERO:
                errors.append(f"negative_balance:{token}")
        return not errors, errors


class ExecutionVerifier:
    """Post-execution verification. It records facts; it never signs."""

    @staticmethod
    def verify(before: dict[str, Decimal], after: dict[str, Decimal], costs: Decimal) -> dict[str, Any]:
        before_total = sum(before.values(), ZERO)
        after_total = sum(after.values(), ZERO)
        realized = after_total - before_total - costs
        return {
            "verified": realized >= ZERO,
            "realized_net": str(realized),
            "before_total": str(before_total),
            "after_total": str(after_total),
            "costs": str(costs),
        }


class LearningEngine:
    def __init__(self, max_samples: int = 256) -> None:
        self.max_samples = max(32, int(max_samples))
        self.samples: list[dict[str, Any]] = []

    def observe(self, *, chain_id: int, predicted: Decimal, realized: Decimal, success: bool) -> None:
        self.samples.append({
            "ts": time.time(),
            "chain_id": int(chain_id),
            "predicted": str(predicted),
            "realized": str(realized),
            "error": str(realized - predicted),
            "success": bool(success),
        })
        self.samples = self.samples[-self.max_samples:]

    def snapshot(self) -> dict[str, Any]:
        if not self.samples:
            return {"samples": 0, "mean_absolute_error": "0"}
        errors = [abs(D(x["error"])) for x in self.samples]
        mae = sum(errors, ZERO) / Decimal(len(errors))
        return {"samples": len(self.samples), "mean_absolute_error": str(mae)}


class ThreeBrainEngines:
    """Top-level orchestration boundary for Dragon's three main brain engines.

    Specialized brains remain implementation modules, but are governed through:
    1. Discovery: discover, quote, and optimize executable opportunities.
    2. Economics: calculate all costs, risk, and expected value.
    3. Execution: prove atomic repayment/profit invariants before execution.

    This boundary never signs or broadcasts transactions.
    """

    def __init__(self, *, discovery, economics, execution, min_profit: Decimal = MIN_PROFIT) -> None:
        self.min_profit = D(min_profit, MIN_PROFIT)
        if self.min_profit < MIN_PROFIT:
            raise ValueError(f"min_profit cannot be below {MIN_PROFIT}")
        self.discovery = discovery
        self.economics = economics
        self.execution = execution
        self.last_stage = "idle"

    def snapshot(self) -> dict[str, Any]:
        return {
            "architecture": "three_brain_engines",
            "min_profit": str(self.min_profit),
            "stage": self.last_stage,
            "engines": {
                "discovery": {
                    "role": "discover_quote_size_profit_curve",
                    "component": type(self.discovery).__name__,
                },
                "economics": {
                    "role": "calculate_rank_risk_select",
                    "component": type(self.economics).__name__,
                },
                "execution": {
                    "role": "prove_simulate_atomic_gate",
                    "component": type(self.execution).__name__,
                },
            },
            "invariant": "DISCOVER -> ECONOMICS -> EXECUTION -> VERIFY",
        }


class DragonCore:
    """Single orchestration object used by the Render process."""

    def __init__(self, min_profit: Decimal = MIN_PROFIT) -> None:
        self.min_profit = max(MIN_PROFIT, D(min_profit, MIN_PROFIT))
        self.world = WorldModel()
        self.market = MarketGraph()
        self.solver = EconomicSolver(self.min_profit)
        self.risk = RiskEngine(self.min_profit)
        self.transitions = StateTransitionEngine()
        self.learning = LearningEngine()
        self.last_decisions: list[EconomicDecision] = []
        self.last_risk: list[dict[str, Any]] = []

    def observe_chain(self, state: ChainState) -> None:
        self.world.update_chain(state)

    def observe_opportunity(self, candidate: EconomicCandidate) -> None:
        self.last_decisions = self.solver.optimize([candidate])

    def rank(self, candidates: Iterable[EconomicCandidate]) -> list[EconomicDecision]:
        rows = []
        self.last_risk = []
        for candidate in candidates:
            chain = self.world.chains.get(candidate.chain_id)
            risk = self.risk.assess(candidate, chain)
            self.last_risk.append({
                "chain_id": candidate.chain_id,
                "allowed": risk.allowed,
                "probability": str(risk.probability),
                "reasons": list(risk.reasons),
            })
            if risk.allowed:
                rows.append(EconomicCandidate(
                    **{**candidate.__dict__, "execution_probability": risk.probability}
                ))
        self.last_decisions = self.solver.optimize(rows)
        return self.last_decisions

    def snapshot(self) -> dict[str, Any]:
        return {
            "min_profit": str(self.min_profit),
            "world": self.world.snapshot(),
            "market": {
                "edges": len(self.market.edges),
            },
            "risk": self.last_risk[-50:],
            "decisions": [
                {
                    "chain_id": d.candidate.chain_id,
                    "source": d.candidate.source,
                    "route": list(d.candidate.route),
                    "quantity": str(d.quantity),
                    "net_profit": str(d.net_profit),
                    "expected_net_value": str(d.expected_net_value),
                    "execution_probability": str(d.execution_probability),
                    "priority": str(d.priority),
                    "reason": d.reason,
                }
                for d in self.last_decisions[:50]
            ],
            "learning": self.learning.snapshot(),
        }
