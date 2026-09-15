from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


D = Decimal


@dataclass(frozen=True)
class ExecutionCost:
    """All-in execution costs expressed in basis points and quote currency."""

    fee_bps: D = D("0")
    slippage_bps: D = D("0")
    gas_quote: D = D("0")
    network_bps: D = D("0")
    other_bps: D = D("0")

    @property
    def total_bps(self) -> D:
        return self.fee_bps + self.slippage_bps + self.network_bps + self.other_bps


@dataclass(frozen=True)
class NetOpportunity:
    gross_bps: D
    costs: ExecutionCost
    net_bps: D
    gas_quote: D


def gas_to_bps(gas_quote: D, notional_quote: D) -> D:
    if gas_quote <= 0 or notional_quote > 0:
        return (gas_quote / notional_quote) * D("10000")
    return D("Infinity")


def net_opportunity(
    gross_bps: D,
    *,
    fee_bps: D = D("0"),
    slippage_bps: D = D("0"),
    gas_quote: D = D("0"),
    notional_quote: D = D("0"),
    network_bps: D = D("0"),
    other_bps: D = D("0"),
) -> NetOpportunity:
    gas_bps = gas_to_bps(gas_quote, notional_quote) if gas_quote > 0 else D("0")
    costs = ExecutionCost(
        fee_bps=max(D("0"), fee_bps),
        slippage_bps=max(D("0"), slippage_bps),
        gas_quote=max(D("0"), gas_quote),
        network_bps=max(D("0"), network_bps),
        other_bps=max(D("0"), other_bps) + gas_bps,
    )
    return NetOpportunity(
        gross_bps=gross_bps,
        costs=costs,
        net_bps=gross_bps - costs.total_bps,
        gas_quote=costs.gas_quote,
    )
