"""Aerodrome capital-allocation intelligence.

Read-only decision layer. It never deposits, stakes, votes, or executes.
It combines pool economics with Dragon's observed arbitrage interaction so
capital can be ranked by expected economic return before any deployment.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from decimal import Decimal, InvalidOperation
import os
from typing import Iterable


D = Decimal


def _d(value, default="0") -> D:
    try:
        return D(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return D(default)


def _env_d(name: str, default: str) -> D:
    return _d(os.getenv(name, default), default)


@dataclass(frozen=True)
class AerodromePoolSnapshot:
    pool: str
    token0: str
    token1: str
    tvl_usd: D
    volume_24h_usd: D
    fees_24h_usd: D
    aero_rewards_24h_usd: D = D("0")
    voting_incentives_24h_usd: D = D("0")
    required_voting_power_usd: D = D("0")
    estimated_il_24h_usd: D = D("0")
    gas_24h_usd: D = D("0")
    arbitrage_volume_24h_usd: D = D("0")
    fee_bps: D = D("0")
    stable: bool = False

    @property
    def total_yield_24h_usd(self) -> D:
        return (
            self.fees_24h_usd
            + self.aero_rewards_24h_usd
            + self.voting_incentives_24h_usd
            - self.estimated_il_24h_usd
            - self.gas_24h_usd
        )

    @property
    def capital_efficiency_24h(self) -> D:
        if self.tvl_usd <= 0:
            return D("0")
        return self.total_yield_24h_usd / self.tvl_usd

    @property
    def arbitrage_interaction_bps(self) -> D:
        if self.tvl_usd <= 0:
            return D("0")
        return (self.arbitrage_volume_24h_usd / self.tvl_usd) * D("10000")

    @property
    def voting_return_24h(self) -> D:
        if self.required_voting_power_usd <= 0:
            return D("0")
        return (
            self.voting_incentives_24h_usd
            + self.aero_rewards_24h_usd
        ) / self.required_voting_power_usd


class AerodromeOpportunityEngine:
    """Ranks Aerodrome pools without deploying capital."""

    def __init__(self, min_tvl_usd: D | None = None):
        self.min_tvl_usd = (
            min_tvl_usd
            if min_tvl_usd is not None
            else _env_d("AERODROME_MIN_TVL_USD", "1000")
        )
        self.arb_weight = _env_d("AERODROME_ARB_INTERACTION_WEIGHT", "0.20")
        self.lp_weight = _env_d("AERODROME_LP_RETURN_WEIGHT", "0.55")
        self.vote_weight = _env_d("AERODROME_VOTE_RETURN_WEIGHT", "0.25")

    def evaluate(self, pool: AerodromePoolSnapshot) -> dict:
        if pool.tvl_usd < self.min_tvl_usd:
            return {
                "pool": pool.pool,
                "status": "filtered",
                "reason": "tvl_below_minimum",
                "score": "0",
            }

        lp_bps = pool.capital_efficiency_24h * D("10000")
        arb_bps = pool.arbitrage_interaction_bps
        vote_bps = pool.voting_return_24h * D("10000")

        score = (
            lp_bps * self.lp_weight
            + arb_bps * self.arb_weight
            + vote_bps * self.vote_weight
        )

        return {
            "pool": pool.pool,
            "pair": f"{pool.token0}/{pool.token1}",
            "stable": pool.stable,
            "status": "candidate",
            "tvl_usd": str(pool.tvl_usd),
            "volume_24h_usd": str(pool.volume_24h_usd),
            "fees_24h_usd": str(pool.fees_24h_usd),
            "aero_rewards_24h_usd": str(pool.aero_rewards_24h_usd),
            "voting_incentives_24h_usd": str(pool.voting_incentives_24h_usd),
            "estimated_il_24h_usd": str(pool.estimated_il_24h_usd),
            "arbitrage_volume_24h_usd": str(pool.arbitrage_volume_24h_usd),
            "net_pool_return_24h_usd": str(pool.total_yield_24h_usd),
            "lp_return_bps_24h": str(lp_bps),
            "arbitrage_interaction_bps": str(arb_bps),
            "voting_return_bps_24h": str(vote_bps),
            "capital_score": str(score),
            "deployment_allowed": False,
        }

    def rank(self, pools: Iterable[AerodromePoolSnapshot]) -> list[dict]:
        rows = [self.evaluate(pool) for pool in pools]
        return sorted(
            rows,
            key=lambda row: _d(row.get("capital_score", "0")),
            reverse=True,
        )


def snapshot_from_dict(data: dict) -> AerodromePoolSnapshot:
    return AerodromePoolSnapshot(
        pool=str(data.get("pool", "")),
        token0=str(data.get("token0", "")),
        token1=str(data.get("token1", "")),
        tvl_usd=_d(data.get("tvl_usd")),
        volume_24h_usd=_d(data.get("volume_24h_usd")),
        fees_24h_usd=_d(data.get("fees_24h_usd")),
        aero_rewards_24h_usd=_d(data.get("aero_rewards_24h_usd")),
        voting_incentives_24h_usd=_d(data.get("voting_incentives_24h_usd")),
        required_voting_power_usd=_d(data.get("required_voting_power_usd")),
        estimated_il_24h_usd=_d(data.get("estimated_il_24h_usd")),
        gas_24h_usd=_d(data.get("gas_24h_usd")),
        arbitrage_volume_24h_usd=_d(data.get("arbitrage_volume_24h_usd")),
        fee_bps=_d(data.get("fee_bps")),
        stable=bool(data.get("stable", False)),
    )
