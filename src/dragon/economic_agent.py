from __future__ import annotations

"""Dynamic economic decision agent for Dragon.

The agent is deliberately separate from quote discovery and transaction execution.
It converts live opportunity/chain observations into an execution priority using
real economic quantities: net profit, gas, flash cost, liquidity, freshness,
latency, RPC reliability and observed execution success.

It never signs or broadcasts transactions.
"""

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable, Sequence


def _d(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        v = Decimal(str(value))
        return v if v.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default


def _clamp01(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


@dataclass(frozen=True)
class ChainMarketState:
    chain_id: int
    gas_quote: Decimal = Decimal("0")
    gas_volatility_bps: Decimal = Decimal("0")
    rpc_success_rate: Decimal = Decimal("1")
    route_success_rate: Decimal = Decimal("1")
    quote_latency_ms: Decimal = Decimal("0")
    block_age_ms: Decimal = Decimal("0")
    flash_available_quote: Decimal = Decimal("0")
    flash_fee_bps: Decimal = Decimal("0")
    liquidity_factor: Decimal = Decimal("1")
    sponsor_cost_quote: Decimal = Decimal("0")
    mev_cost_quote: Decimal = Decimal("0")


@dataclass(frozen=True)
class EconomicOrder:
    opportunity: object
    chain_id: int
    expected_net_profit_quote: Decimal
    net_profit_quote: Decimal
    execution_probability: Decimal
    freshness_factor: Decimal
    latency_factor: Decimal
    capital_efficiency: Decimal
    economic_priority: Decimal
    reason: str


@dataclass
class _ChainMemory:
    latency_ms: deque
    success: deque
    realized_net: deque
    updated_at: float = 0.0


class EconomicDecisionAgent:
    """Continuously updated economic selector for Dragon.

    Discovery remains broad. This agent decides which already-quoted opportunity
    should receive the next expensive simulation/order slot.
    """

    def __init__(self, *, history_size: int = 256, min_profit: Decimal = Decimal("0.002")):
        self.history_size = max(32, int(history_size))
        self.min_profit = _d(min_profit, Decimal("0.002"))
        if self.min_profit < Decimal("0.002"):
            raise ValueError("min_profit cannot be below 0.002")
        self._memory: dict[int, _ChainMemory] = {}

    def _memory_for(self, chain_id: int) -> _ChainMemory:
        if chain_id not in self._memory:
            self._memory[chain_id] = _ChainMemory(
                latency_ms=deque(maxlen=self.history_size),
                success=deque(maxlen=self.history_size),
                realized_net=deque(maxlen=self.history_size),
            )
        return self._memory[chain_id]

    def observe_quote(self, *, chain_id: int, latency_ms: Decimal, success: bool) -> None:
        m = self._memory_for(int(chain_id))
        latency = _d(latency_ms, Decimal("Infinity"))
        if latency.is_finite() and latency >= 0:
            m.latency_ms.append(latency)
        m.success.append(bool(success))
        m.updated_at = time.time()

    def observe_execution(self, *, chain_id: int, success: bool, realized_net_quote: Decimal = Decimal("0"), latency_ms: Decimal = Decimal("0")) -> None:
        m = self._memory_for(int(chain_id))
        m.success.append(bool(success))
        if success:
            net = _d(realized_net_quote)
            if net.is_finite():
                m.realized_net.append(net)
        latency = _d(latency_ms, Decimal("Infinity"))
        if latency.is_finite() and latency >= 0:
            m.latency_ms.append(latency)
        m.updated_at = time.time()

    def chain_reliability(self, chain_id: int, fallback: Decimal = Decimal("1")) -> Decimal:
        m = self._memory.get(int(chain_id))
        if not m or not m.success:
            return _clamp01(fallback)
        return _clamp01(Decimal(sum(m.success)) / Decimal(len(m.success)))

    def chain_latency(self, chain_id: int, fallback: Decimal = Decimal("0")) -> Decimal:
        m = self._memory.get(int(chain_id))
        if not m or not m.latency_ms:
            return max(Decimal("0"), fallback)
        return sum(m.latency_ms, Decimal("0")) / Decimal(len(m.latency_ms))

    @staticmethod
    def _freshness(age_ms: Decimal, block_age_ms: Decimal) -> Decimal:
        age = max(Decimal("0"), age_ms)
        block_age = max(Decimal("0"), block_age_ms)
        # Exponential decay avoids a discontinuous cliff while making stale
        # opportunities progressively less valuable.
        return Decimal(str(math.exp(-float((age + block_age) / Decimal("1000")))))

    @staticmethod
    def _latency_factor(latency_ms: Decimal) -> Decimal:
        latency = max(Decimal("0"), latency_ms)
        return Decimal(str(math.exp(-float(latency / Decimal("1000")))))

    def evaluate(self, opportunity: object, state: ChainMarketState | None = None) -> EconomicOrder | None:
        chain_id = int(getattr(opportunity, "chain_id", getattr(getattr(opportunity, "route", None), "chain_id", 0)))
        net = _d(getattr(opportunity, "net_profit_quote", 0), Decimal("-Infinity"))
        if not net.is_finite() or net < self.min_profit:
            return None

        if state is None:
            state = ChainMarketState(chain_id=chain_id)

        observed_latency = self.chain_latency(chain_id, state.quote_latency_ms)
        reliability = self.chain_reliability(chain_id, state.route_success_rate)
        rpc = _clamp01(state.rpc_success_rate)
        execution_probability = _clamp01(reliability * rpc)

        quote_age = _d(getattr(opportunity, "quote_age_ms", 0))
        freshness = self._freshness(quote_age, state.block_age_ms)
        latency_factor = self._latency_factor(observed_latency)

        liquidity = _clamp01(state.liquidity_factor)
        gas_vol = Decimal("1") / (Decimal("1") + max(Decimal("0"), state.gas_volatility_bps) / Decimal("10000"))

        # net_profit_quote is already costed. We therefore do not subtract gas or
        # flash fees a second time; those inputs influence the confidence/priority
        # and must also be visible in the underlying opportunity economics.
        expected = net * execution_probability * freshness * latency_factor * liquidity * gas_vol
        amount = _d(getattr(opportunity, "quote_amount", 0))
        density = expected / amount if amount > 0 else Decimal("-Infinity")

        # Economic priority is expected executable value, with profit density as
        # the deterministic tie-breaker. No arbitrary risk score is introduced.
        priority = expected + max(Decimal("0"), density) * Decimal("0.000001")

        return EconomicOrder(
            opportunity=opportunity,
            chain_id=chain_id,
            expected_net_profit_quote=expected,
            net_profit_quote=net,
            execution_probability=execution_probability,
            freshness_factor=freshness,
            latency_factor=latency_factor,
            capital_efficiency=density,
            economic_priority=priority,
            reason=(
                f"net={net};expected={expected};execution_probability={execution_probability};"
                f"freshness={freshness};latency_factor={latency_factor};"
                f"liquidity={liquidity};gas_volatility_factor={gas_vol}"
            ),
        )

    def rank(
        self,
        opportunities: Sequence[object],
        chain_states: dict[int, ChainMarketState] | None = None,
    ) -> list[EconomicOrder]:
        chain_states = chain_states or {}
        rows: list[EconomicOrder] = []
        for opportunity in opportunities:
            chain_id = int(getattr(opportunity, "chain_id", getattr(getattr(opportunity, "route", None), "chain_id", 0)))
            row = self.evaluate(opportunity, chain_states.get(chain_id))
            if row is not None:
                rows.append(row)
        return sorted(
            rows,
            key=lambda x: (
                x.economic_priority,
                x.expected_net_profit_quote,
                x.capital_efficiency,
                -x.chain_id,
            ),
            reverse=True,
        )

    def prioritize_chains(self, chain_ids: Iterable[int], probe_scores: dict[int, Decimal]) -> list[int]:
        """Order discovery work by live probe economics without disabling any chain."""
        rows = []
        for chain_id in chain_ids:
            score = _d(probe_scores.get(int(chain_id), Decimal("-Infinity")), Decimal("-Infinity"))
            reliability = self.chain_reliability(int(chain_id))
            latency = self.chain_latency(int(chain_id))
            latency_factor = self._latency_factor(latency)
            value = score * reliability * latency_factor if score.is_finite() else Decimal("-Infinity")
            rows.append((value, int(chain_id)))
        return [cid for _, cid in sorted(rows, reverse=True)]

    def snapshot(self) -> dict:
        return {
            str(cid): {
                "reliability": str(self.chain_reliability(cid)),
                "latency_ms": str(self.chain_latency(cid)),
                "samples": len(memory.success),
                "updated_at": memory.updated_at,
            }
            for cid, memory in self._memory.items()
        }
