from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


def _dec(value: Any) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")
    return d if d.is_finite() else Decimal("0")


@dataclass(frozen=True)
class OracleSnapshot:
    chain_id: int
    asset: str
    oracle: str
    source: str
    price: Decimal
    oracle_decimals: int
    expected_decimals: int
    valid: bool
    is_capped: bool | None = None
    snapshot_ratio: Decimal | None = None
    snapshot_timestamp: int | None = None
    max_yearly_growth_pct: Decimal | None = None
    adapter_description: str | None = None

    @property
    def cap_state(self) -> str:
        if self.is_capped is True:
            return "capped"
        if self.is_capped is False:
            return "uncapped"
        return "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "asset": self.asset,
            "oracle": self.oracle,
            "source": self.source,
            "price": str(self.price),
            "oracle_decimals": self.oracle_decimals,
            "expected_decimals": self.expected_decimals,
            "valid": self.valid,
            "cap_state": self.cap_state,
            "snapshot_ratio": None if self.snapshot_ratio is None else str(self.snapshot_ratio),
            "snapshot_timestamp": self.snapshot_timestamp,
            "max_yearly_growth_pct": (
                None if self.max_yearly_growth_pct is None else str(self.max_yearly_growth_pct)
            ),
            "adapter_description": self.adapter_description,
        }


@dataclass(frozen=True)
class OracleDecision:
    allowed: bool
    reason: str
    deviation_bps: Decimal | None
    reference_price: Decimal | None
    oracle_price: Decimal | None
    source: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "deviation_bps": None if self.deviation_bps is None else str(self.deviation_bps),
            "reference_price": None if self.reference_price is None else str(self.reference_price),
            "oracle_price": None if self.oracle_price is None else str(self.oracle_price),
            "source": self.source,
        }


def compare_reference(
    snapshot: OracleSnapshot | None,
    *,
    reference_price: Decimal | str | float,
    max_deviation_bps: Decimal | str | float = Decimal("0"),
    require_oracle: bool = False,
) -> OracleDecision:
    ref = _dec(reference_price)
    if snapshot is None:
        return OracleDecision(
            allowed=not require_oracle,
            reason="oracle_missing" if require_oracle else "oracle_not_configured",
            deviation_bps=None,
            reference_price=ref,
            oracle_price=None,
            source=None,
        )
    if not snapshot.valid or snapshot.price <= 0:
        return OracleDecision(
            allowed=False,
            reason="oracle_invalid",
            deviation_bps=None,
            reference_price=ref,
            oracle_price=snapshot.price,
            source=snapshot.source,
        )
    if ref <= 0:
        return OracleDecision(
            allowed=False,
            reason="reference_price_invalid",
            deviation_bps=None,
            reference_price=ref,
            oracle_price=snapshot.price,
            source=snapshot.source,
        )

    deviation = abs(snapshot.price - ref) / snapshot.price * Decimal("10000")
    limit = _dec(max_deviation_bps)
    if limit > 0 and deviation > limit:
        return OracleDecision(
            allowed=False,
            reason="oracle_deviation_too_large",
            deviation_bps=deviation,
            reference_price=ref,
            oracle_price=snapshot.price,
            source=snapshot.source,
        )

    return OracleDecision(
        allowed=True,
        reason="oracle_ok",
        deviation_bps=deviation,
        reference_price=ref,
        oracle_price=snapshot.price,
        source=snapshot.source,
    )


def is_price_feed_candidate(description: str | None) -> bool:
    text = (description or "").lower()
    return any(token in text for token in ("usd", "eth", "cap", "synchronicity", "chainlink"))


AAVE_PROTOCOL_INTELLIGENCE = {
    "price_feeds_repository": "https://github.com/aave-dao/aave-price-feeds",
    "parameters_url": "https://aave.com/docs/resources/parameters",
    "changelog_url": "https://aave.com/docs/resources/changelog",
    "oracle_rule": "latestAnswer + decimals compatibility; CAPO state is informational unless configured as a hard guard",
    "changelog_refresh": "background/off-path",
}
