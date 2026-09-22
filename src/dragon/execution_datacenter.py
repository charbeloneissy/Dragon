"""Fast execution memory and capacity controller for Cloud B.

The hot path uses bounded in-memory rolling state. A durable data center can
persist the records asynchronously; persistence must never block execution.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ExecutionRecord:
    execution_id: str
    chain_id: int
    route: str
    leg: int
    success: bool
    gas_native: int
    gas_cost_quote: Decimal
    realized_net_quote: Decimal | None
    latency_ms: Decimal
    timestamp: float


class ExecutionDataCenter:
    def __init__(self, *, history_size: int = 2048):
        self.records = deque(maxlen=max(64, history_size))

    def record(self, item: ExecutionRecord) -> None:
        self.records.append(item)

    def average_gas_quote(self, *, chain_id: int | None = None, leg: int | None = None) -> Decimal:
        rows = [r for r in self.records if (chain_id is None or r.chain_id == chain_id) and
                (leg is None or r.leg == leg)]
        if not rows:
            return Decimal("0")
        return sum((r.gas_cost_quote for r in rows), Decimal("0")) / Decimal(len(rows))

    def success_rate(self, *, chain_id: int | None = None) -> Decimal:
        rows = [r for r in self.records if chain_id is None or r.chain_id == chain_id]
        return (Decimal(sum(r.success for r in rows)) / Decimal(len(rows))) if rows else Decimal("1")


class ExecutionCapacity:
    """Adaptive concurrency; no artificial daily trade-count limit."""

    def __init__(self, *, initial: int = 8, minimum: int = 1, maximum: int | None = None):
        self.current = max(minimum, initial)
        self.minimum = minimum
        self.maximum = maximum

    def observe(self, *, rpc_ok: bool, latency_ms: Decimal, pending: int, capacity_hint: int | None = None) -> int:
        if capacity_hint is not None:
            target = max(self.minimum, int(capacity_hint)) if self.maximum is None else max(self.minimum, min(int(capacity_hint), self.maximum))
            self.current = target
            return target
        if not rpc_ok or pending > self.current * 2:
            self.current = max(self.minimum, self.current // 2)
        elif latency_ms > Decimal("1000"):
            self.current = max(self.minimum, self.current - 1)
        elif latency_ms < Decimal("250") and pending < self.current:
            self.current = self.current + 1 if self.maximum is None else min(self.maximum, self.current + 1)
        return self.current
