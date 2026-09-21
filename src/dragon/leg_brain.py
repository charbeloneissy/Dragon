"""Independent ultra-smart intelligence for a single arbitrage leg.

The leg brain is deliberately chain/venue agnostic. It consumes live observations,
keeps short rolling execution statistics, and produces a deterministic decision
packet for the route composer. It never signs or broadcasts transactions.
"""
from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal


def _d(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        x = Decimal(str(value))
        return x if x.is_finite() else default
    except Exception:
        return default


@dataclass(frozen=True)
class LegObservation:
    chain_id: int
    source: str
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    gas_cost_quote: Decimal = Decimal("0")
    latency_ms: Decimal = Decimal("0")
    quote_age_ms: Decimal = Decimal("0")
    liquidity_ok: bool = True
    block_number: int = 0
    observed_at: float = 0.0


@dataclass(frozen=True)
class LegDecision:
    executable: bool
    chain_id: int
    source: str
    sell_amount: int
    buy_amount: int
    gas_cost_quote: Decimal
    latency_ms: Decimal
    quote_age_ms: Decimal
    reason: str


class LegBrain:
    """Independent stateful brain for one route leg."""

    def __init__(self, *, max_quote_age_ms: Decimal = Decimal("500"), history_size: int = 256):
        self.max_quote_age_ms = _d(max_quote_age_ms, Decimal("500"))
        self._latency = deque(maxlen=max(8, history_size))
        self._gas = deque(maxlen=max(8, history_size))
        self._success = deque(maxlen=max(8, history_size))

    def observe(self, observation: LegObservation) -> None:
        self._latency.append(_d(observation.latency_ms))
        self._gas.append(_d(observation.gas_cost_quote))
        self._success.append(bool(observation.liquidity_ok))

    @property
    def average_latency_ms(self) -> Decimal:
        return (sum(self._latency, Decimal("0")) / Decimal(len(self._latency))) if self._latency else Decimal("0")

    @property
    def average_gas_quote(self) -> Decimal:
        return (sum(self._gas, Decimal("0")) / Decimal(len(self._gas))) if self._gas else Decimal("0")

    @property
    def success_rate(self) -> Decimal:
        return (Decimal(sum(self._success)) / Decimal(len(self._success))) if self._success else Decimal("1")

    def decide(self, observation: LegObservation, *, now: float | None = None) -> LegDecision:
        now = time.time() if now is None else now
        age = _d(observation.quote_age_ms)
        if observation.sell_amount <= 0 or observation.buy_amount <= 0:
            return LegDecision(False, observation.chain_id, observation.source, observation.sell_amount,
                               observation.buy_amount, _d(observation.gas_cost_quote),
                               _d(observation.latency_ms), age, "non_positive_amount")
        if not observation.liquidity_ok:
            return LegDecision(False, observation.chain_id, observation.source, observation.sell_amount,
                               observation.buy_amount, _d(observation.gas_cost_quote),
                               _d(observation.latency_ms), age, "liquidity_unavailable")
        if age > self.max_quote_age_ms:
            return LegDecision(False, observation.chain_id, observation.source, observation.sell_amount,
                               observation.buy_amount, _d(observation.gas_cost_quote),
                               _d(observation.latency_ms), age, "quote_expired")
        if not math.isfinite(float(_d(observation.latency_ms))):
            return LegDecision(False, observation.chain_id, observation.source, observation.sell_amount,
                               observation.buy_amount, _d(observation.gas_cost_quote),
                               _d(observation.latency_ms), age, "invalid_latency")
        self.observe(observation)
        return LegDecision(True, observation.chain_id, observation.source, observation.sell_amount,
                           observation.buy_amount, _d(observation.gas_cost_quote),
                           _d(observation.latency_ms), age, "live_quote_validated")
