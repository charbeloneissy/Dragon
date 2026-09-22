from __future__ import annotations

"""Dynamic economic calculator for Dragon.

The calculator owns size/flash-asset economics. It does not execute anything.
Every value must come from live quote, chain, flash-liquidity, or execution data.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


def _d(value: object, default: Decimal | None = None) -> Decimal:
    try:
        v = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        if default is None:
            raise
        return default
    if not v.is_finite():
        if default is None:
            raise ValueError("non-finite decimal")
        return default
    return v


@dataclass(frozen=True)
class ChainEconomics:
    chain_id: int
    gas_native: Decimal
    native_to_quote: Decimal
    gas_volatility_bps: Decimal = Decimal("0")
    latency_ms: Decimal = Decimal("0")
    rpc_success_rate: Decimal = Decimal("1")

    @property
    def gas_quote(self) -> Decimal:
        if self.gas_native < 0 or self.native_to_quote <= 0:
            return Decimal("Infinity")
        return self.gas_native * self.native_to_quote


@dataclass(frozen=True)
class FlashLiquidity:
    chain_id: int
    asset: str
    available_quote: Decimal
    fee_bps: Decimal
    liquidity_confidence: Decimal = Decimal("1")


@dataclass(frozen=True)
class RouteEconomics:
    input_quote: Decimal
    final_quote: Decimal
    dex_fees_quote: Decimal
    slippage_quote: Decimal
    gas_quote: Decimal
    flash_fee_quote: Decimal
    sponsor_cost_quote: Decimal
    mev_cost_quote: Decimal = Decimal("0")
    execution_loss_quote: Decimal = Decimal("0")


@dataclass(frozen=True)
class CalculationResult:
    flash_asset: str
    amount_quote: Decimal
    gross_profit_quote: Decimal
    total_cost_quote: Decimal
    net_profit_quote: Decimal
    profit_bps: Decimal
    capital_efficiency: Decimal
    executable: bool
    reason: str


class EconomicCalculator:
    def __init__(self, *, min_profit: Decimal = Decimal("0.002")):
        self.min_profit = _d(min_profit)
        if self.min_profit < Decimal("0.002"):
            raise ValueError("min_profit cannot be below 0.002")

    def calculate(
        self,
        *,
        flash: FlashLiquidity,
        route: RouteEconomics,
        chain: ChainEconomics,
    ) -> CalculationResult:
        amount = _d(route.input_quote)
        if amount <= 0 or flash.available_quote < amount:
            return CalculationResult(flash.asset, amount, Decimal("0"), Decimal("0"),
                                     Decimal("-Infinity"), Decimal("-Infinity"),
                                     Decimal("-Infinity"), False, "insufficient_flash_liquidity")
        if flash.fee_bps < 0 or flash.fee_bps > Decimal("1000"):
            return CalculationResult(flash.asset, amount, Decimal("0"), Decimal("0"),
                                     Decimal("-Infinity"), Decimal("-Infinity"),
                                     Decimal("-Infinity"), False, "invalid_flash_fee")
        if chain.gas_quote <= 0 or not chain.gas_quote.is_finite():
            return CalculationResult(flash.asset, amount, Decimal("0"), Decimal("0"),
                                     Decimal("-Infinity"), Decimal("-Infinity"),
                                     Decimal("-Infinity"), False, "invalid_chain_gas")

        gross = route.final_quote - amount
        flash_fee = amount * flash.fee_bps / Decimal("10000")
        total = (
            route.dex_fees_quote + route.slippage_quote + chain.gas_quote
            + flash_fee + route.sponsor_cost_quote + route.mev_cost_quote
            + route.execution_loss_quote
        )
        net = gross - total
        bps = (net / amount) * Decimal("10000") if amount > 0 else Decimal("-Infinity")
        density = net / amount if amount > 0 else Decimal("-Infinity")
        executable = net >= self.min_profit and flash.liquidity_confidence > 0
        return CalculationResult(
            flash_asset=flash.asset,
            amount_quote=amount,
            gross_profit_quote=gross,
            total_cost_quote=total,
            net_profit_quote=net,
            profit_bps=bps,
            capital_efficiency=density,
            executable=executable,
            reason=(
                f"gross={gross};dex={route.dex_fees_quote};slippage={route.slippage_quote};"
                f"gas={chain.gas_quote};flash={flash_fee};sponsor={route.sponsor_cost_quote};"
                f"mev={route.mev_cost_quote};execution_loss={route.execution_loss_quote}"
            ),
        )

    def optimize(
        self,
        *,
        flashes: Iterable[FlashLiquidity],
        chains: Iterable[ChainEconomics],
        route_builder,
        candidate_amounts: Iterable[Decimal],
    ) -> CalculationResult | None:
        """Evaluate flash asset × chain × size combinations.

        route_builder(flash, chain, amount) must return fresh RouteEconomics.
        The route builder is responsible for live DEX re-quotes; this method
        never extrapolates stale quotes.
        """
        best: CalculationResult | None = None
        for flash in flashes:
            for chain in chains:
                if flash.chain_id != chain.chain_id:
                    continue
                for amount in candidate_amounts:
                    amount = _d(amount)
                    if amount <= 0 or amount > flash.available_quote:
                        continue
                    route = route_builder(flash, chain, amount)
                    if route is None:
                        continue
                    result = self.calculate(flash=flash, route=route, chain=chain)
                    if best is None or result.net_profit_quote > best.net_profit_quote:
                        best = result
        return best
