from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from src.dragon.aave_mcp import AaveMarketSnapshot


@dataclass(frozen=True)
class AaveSupplyCandidate:
    reserve_id: str
    chain_id: int
    chain: str
    symbol: str
    supply_apy_pct: Decimal
    displayed_apy_pct: Decimal
    can_use_as_collateral: bool
    suppliable: Decimal | None

    def estimated_annual_yield(self, amount: Decimal) -> Decimal:
        if amount <= 0:
            raise ValueError("amount must be positive")
        return amount * self.supply_apy_pct / Decimal("100")

    def as_dict(self) -> dict:
        return {
            "reserve_id": self.reserve_id,
            "chain_id": self.chain_id,
            "chain": self.chain,
            "symbol": self.symbol,
            "supply_apy_pct": str(self.supply_apy_pct),
            "displayed_apy_pct": str(self.displayed_apy_pct),
            "can_use_as_collateral": self.can_use_as_collateral,
            "suppliable": None if self.suppliable is None else str(self.suppliable),
        }


def find_enterable_supply_candidates(
    rows: Iterable[AaveMarketSnapshot],
    *,
    require_collateral: bool = False,
) -> list[AaveSupplyCandidate]:
    candidates: list[AaveSupplyCandidate] = []
    for row in rows:
        if row.version not in {"v3", "v4"}:
            continue
        if row.reserve_id is None or row.chain_id is None:
            continue
        if row.supply_apy_pct is None:
            continue
        if row.can_supply is False or row.frozen or row.paused:
            continue
        if row.suppliable is not None and row.suppliable <= 0:
            continue
        if require_collateral and row.can_use_as_collateral is not True:
            continue

        displayed = row.displayed_apy_pct or row.supply_apy_pct
        candidates.append(
            AaveSupplyCandidate(
                reserve_id=row.reserve_id,
                chain_id=row.chain_id,
                chain=row.chain,
                symbol=row.symbol,
                supply_apy_pct=row.supply_apy_pct,
                displayed_apy_pct=displayed,
                can_use_as_collateral=row.can_use_as_collateral is True,
                suppliable=row.suppliable,
            )
        )

    return sorted(
        candidates,
        key=lambda item: (item.displayed_apy_pct, item.supply_apy_pct),
        reverse=True,
    )
