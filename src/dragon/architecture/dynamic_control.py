from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class DynamicBudget:
    scan_interval_ms: int
    candidate_budget: int
    simulation_budget: int
    quote_refresh_ms: int


class DynamicController:
    """Adjust workload from observed market/system conditions.

    This controls discovery and compute allocation only. It never changes the
    atomic safety invariants or the minimum realized-profit requirement.
    """

    def __init__(self) -> None:
        self._budget = DynamicBudget(30_000, 10, 3, 1_000)

    @property
    def budget(self) -> DynamicBudget:
        return self._budget

    def update(
        self,
        *,
        opportunity_density: Decimal,
        volatility_bps: Decimal,
        rpc_latency_ms: Decimal,
        simulation_queue: int,
    ) -> DynamicBudget:
        density = max(Decimal("0"), Decimal(opportunity_density))
        volatility = max(Decimal("0"), Decimal(volatility_bps))
        latency = max(Decimal("0"), Decimal(rpc_latency_ms))

        if simulation_queue > 20 or latency > 1500:
            candidates, interval = 10, 45_000
        elif density > Decimal("0.50") or volatility > Decimal("300"):
            candidates, interval = 100, 10_000
        elif density > Decimal("0.15") or volatility > Decimal("100"):
            candidates, interval = 50, 20_000
        else:
            candidates, interval = 10, 30_000

        refresh = 500 if latency < 300 else 1_000
        simulation_budget = max(1, min(10, candidates // 10))

        self._budget = DynamicBudget(
            scan_interval_ms=interval,
            candidate_budget=candidates,
            simulation_budget=simulation_budget,
            quote_refresh_ms=refresh,
        )
        return self._budget
