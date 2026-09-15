"""Defensive coordination layer for Dragon's quantitative engines.

This module does not place orders. It provides explicit health/readiness checks
for data, quantitative research, risk controls, and paper/replay state so one
subsystem cannot silently declare the whole system healthy.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class EngineHealth:
    name: str
    enabled: bool
    healthy: bool
    reason: str = ""


@dataclass(frozen=True)
class SystemHealth:
    engines: tuple[EngineHealth, ...]

    @property
    def healthy(self) -> bool:
        return all((not e.enabled) or e.healthy for e in self.engines)

    @property
    def degraded(self) -> bool:
        enabled = [e for e in self.engines if e.enabled]
        return bool(enabled) and any(not e.healthy for e in enabled)


def validate_quant_inputs(*, net_edge_bps: Decimal, expected_pnl_usdt: Decimal,
                          liquidity_utilization: Decimal, latency_penalty_bps: Decimal,
                          max_liquidity_utilization: Decimal,
                          max_latency_penalty_bps: Decimal) -> dict[str, bool]:
    """Return independent research gates; no gate is overridden by score."""
    return {
        "finite_edge": net_edge_bps.is_finite(),
        "positive_expected_pnl": expected_pnl_usdt > 0,
        "liquidity_ok": 0 <= liquidity_utilization <= max_liquidity_utilization,
        "latency_ok": 0 <= latency_penalty_bps <= max_latency_penalty_bps,
    }


def build_system_health(*, market: EngineHealth, quant: EngineHealth,
                        risk: EngineHealth, telemetry: EngineHealth,
                        research: EngineHealth) -> SystemHealth:
    return SystemHealth((market, quant, risk, telemetry, research))
