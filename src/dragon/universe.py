from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .cost_model import net_opportunity


@dataclass(frozen=True)
class VenueLeg:
    venue: str
    kind: str  # CEX_SPOT or DEX_SPOT
    symbol: str
    fee_bps: Decimal
    slippage_bps: Decimal
    gas_quote: Decimal = Decimal("0")
    network_bps: Decimal = Decimal("0")


@dataclass(frozen=True)
class UniverseOpportunity:
    strategy: str  # SPOT_SPOT, TRIANGLE, CEX_DEX, DEX_CEX, DEX_DEX
    path: tuple[str, ...]
    gross_bps: Decimal
    net_bps: Decimal
    cost_bps: Decimal
    gas_quote: Decimal


def evaluate_costs(strategy: str, path: tuple[str, ...], gross_bps: Decimal, legs: list[VenueLeg], notional_quote: Decimal) -> UniverseOpportunity | None:
    fee = sum((x.fee_bps for x in legs), Decimal("0"))
    slippage = sum((x.slippage_bps for x in legs), Decimal("0"))
    gas = sum((x.gas_quote for x in legs), Decimal("0"))
    network = sum((x.network_bps for x in legs), Decimal("0"))
    result = net_opportunity(
        gross_bps,
        fee_bps=fee,
        slippage_bps=slippage,
        gas_quote=gas,
        notional_quote=notional_quote,
        network_bps=network,
    )
    if result.net_bps <= 0:
        return None
    return UniverseOpportunity(
        strategy=strategy,
        path=path,
        gross_bps=gross_bps,
        net_bps=result.net_bps,
        cost_bps=result.costs.total_bps,
        gas_quote=result.gas_quote,
    )


def all_strategy_types() -> tuple[str, ...]:
    return ("SPOT_SPOT", "TRIANGLE", "CEX_DEX", "DEX_CEX", "DEX_DEX")
