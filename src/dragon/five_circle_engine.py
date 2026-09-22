from __future__ import annotations

"""Fast five-circle economic gate for Dragon.

The engine is deliberately small and deterministic. It does not invent prices,
probabilities, or execution results. It consumes the live opportunity objects
already produced by the DEX scanner and applies configurable stress tests.

Circles:
1. DISCOVER   - keep valid candidates from the current rotation.
2. OPTIMIZE   - rank candidates by current net economics.
3. CHALLENGE  - stress gas and execution loss.
4. PROVE      - require a paper/simulation-ready candidate; never claims a
                transaction was simulated when no simulator result exists.
5. EXECUTE    - return an execution gate. In paper mode this is only a
                would-execute decision.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence


def _d(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return default
    return result if result.is_finite() else default


@dataclass(frozen=True)
class DynamicParameters:
    rotation: int
    chain_id: int
    block_number: int
    min_profit_quote: Decimal = Decimal("0.005")
    gas_stress_bps: Decimal = Decimal("2000")
    execution_stress_bps: Decimal = Decimal("100")
    max_candidates: int = 8

    @property
    def gas_multiplier(self) -> Decimal:
        return Decimal("1") + self.gas_stress_bps / Decimal("10000")


@dataclass(frozen=True)
class CircleDecision:
    circle: str
    passed: bool
    candidates: tuple[object, ...]
    reason: str


@dataclass(frozen=True)
class FiveCircleResult:
    rotation: int
    parameters: DynamicParameters
    decisions: tuple[CircleDecision, ...]
    selected: object | None

    @property
    def executable(self) -> bool:
        return self.selected is not None and all(d.passed for d in self.decisions[2:])


class FiveCircleEngine:
    """Incremental, cheap-to-expensive opportunity gate."""

    def __init__(
        self,
        *,
        min_profit: Decimal = Decimal("0.005"),
        gas_stress_bps: Decimal = Decimal("2000"),
        execution_stress_bps: Decimal = Decimal("100"),
        max_candidates: int = 8,
    ) -> None:
        self.min_profit = _d(min_profit)
        self.gas_stress_bps = _d(gas_stress_bps)
        self.execution_stress_bps = _d(execution_stress_bps)
        self.max_candidates = max(1, int(max_candidates))
        if self.min_profit < Decimal("0.002"):
            raise ValueError("min_profit cannot be below 0.002")
        if self.gas_stress_bps < 0 or self.execution_stress_bps < 0:
            raise ValueError("stress parameters cannot be negative")

    def _net(self, opportunity: object) -> Decimal:
        return _d(getattr(opportunity, "net_profit_quote", Decimal("-Infinity")), Decimal("-Infinity"))

    def _gross(self, opportunity: object) -> Decimal:
        return _d(getattr(opportunity, "gross_profit_quote", 0))

    def _gas(self, opportunity: object) -> Decimal:
        return max(Decimal("0"), _d(getattr(opportunity, "gas_cost_quote", 0)))

    def run(
        self,
        opportunities: Iterable[object],
        *,
        rotation: int,
        chain_id: int,
        block_number: int,
        simulation_passed: bool = False,
    ) -> FiveCircleResult:
        source = tuple(
            x for x in opportunities
            if getattr(x, "chain_id", chain_id) == chain_id and self._net(x).is_finite()
        )
        params = DynamicParameters(
            rotation=rotation,
            chain_id=chain_id,
            block_number=block_number,
            min_profit_quote=self.min_profit,
            gas_stress_bps=self.gas_stress_bps,
            execution_stress_bps=self.execution_stress_bps,
            max_candidates=len(source),
        )

        # Circle 1: discover.
        discovered = tuple(sorted(source, key=self._net, reverse=True))
        c1 = CircleDecision(
            "DISCOVER", bool(discovered), discovered,
            "live candidates from current rotation" if discovered else "no valid candidates",
        )
        if not discovered:
            return FiveCircleResult(rotation, params, (c1,), None)

        # Circle 2: optimize.
        optimized = tuple(sorted(discovered, key=lambda x: (self._net(x), -self._gas(x)), reverse=True))
        c2 = CircleDecision("OPTIMIZE", True, optimized, "ranked by current net profit and gas")

        # Circle 3: challenge. Stress only costs we can derive from the object.
        challenged: list[object] = []
        for candidate in optimized:
            base_net = self._net(candidate)
            gas_penalty = self._gas(candidate) * params.gas_stress_bps / Decimal("10000")
            execution_penalty = max(self._gross(candidate), Decimal("0")) * params.execution_stress_bps / Decimal("10000")
            stressed = base_net - gas_penalty - execution_penalty
            if stressed >= params.min_profit:
                challenged.append(candidate)
        challenged_tuple = tuple(challenged)
        c3 = CircleDecision(
            "CHALLENGE",
            bool(challenged_tuple),
            challenged_tuple,
            "survives configured gas and execution stress" if challenged_tuple else "all candidates fail stress gate",
        )
        if not challenged_tuple:
            return FiveCircleResult(rotation, params, (c1, c2, c3), None)

        # Circle 4: prove. Without a real simulator result, this is explicitly a
        # paper/simulation-ready gate, never a false claim of on-chain proof.
        proven = challenged_tuple if simulation_passed else tuple()
        c4 = CircleDecision(
            "PROVE",
            bool(proven),
            proven,
            "simulation result supplied" if simulation_passed else "waiting for exact simulation result",
        )
        if not proven:
            return FiveCircleResult(rotation, params, (c1, c2, c3, c4), None)

        # Circle 5: execution gate.
        selected = max(proven, key=lambda x: (self._net(x), -self._gas(x)))
        c5 = CircleDecision("EXECUTE", True, (selected,), "all five gates passed")
        return FiveCircleResult(rotation, params, (c1, c2, c3, c4, c5), selected)
