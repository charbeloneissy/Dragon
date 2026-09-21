from __future__ import annotations

"""Ultra-economic market intelligence for Dragon.

This module is deliberately quote/execution agnostic. It does not sign or send
transactions and it never invents missing market inputs. It converts live
observations into measurable opportunity economics for the Calculator Brain.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import exp


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
class MarketObservation:
    chain_id: int
    venue_buy: str
    venue_sell: str
    quote_token: str
    base_token: str
    input_amount_quote: Decimal
    output_amount_base: Decimal
    return_amount_quote: Decimal
    quote_age_ms: Decimal
    route_latency_ms: Decimal
    gas_cost_quote: Decimal
    dex_fees_quote: Decimal
    flash_fee_quote: Decimal
    slippage_quote: Decimal
    sponsor_cost_quote: Decimal = Decimal("0")
    liquidity_utilization: Decimal = Decimal("0")
    spread_bps: Decimal = Decimal("0")
    gas_volatility_bps: Decimal = Decimal("0")
    rpc_success_rate: Decimal = Decimal("1")
    route_success_rate: Decimal = Decimal("1")
    spread_survival_ms: Decimal = Decimal("0")
    execution_window_ms: Decimal = Decimal("0")
    mev_cost_quote: Decimal = Decimal("0")


@dataclass(frozen=True)
class MarketDecision:
    executable_economic_value: Decimal
    gross_profit_quote: Decimal
    total_cost_quote: Decimal
    expected_loss_quote: Decimal
    survival_factor: Decimal
    execution_factor: Decimal
    capital_efficiency: Decimal
    reason: str


class MarketBrain:
    """Measures economic opportunity quality using only supplied live factors.

    No arbitrary 'AI score' is used. The output is an economic value estimate
    that the Calculator Brain can use for route/size selection.
    """

    def __init__(self, *, min_profit: Decimal = Decimal("0.005")):
        self.min_profit = _d(min_profit)
        if self.min_profit < Decimal("0.005"):
            raise ValueError("min_profit cannot be below 0.005")

    @staticmethod
    def _clamp01(value: Decimal) -> Decimal:
        return max(Decimal("0"), min(Decimal("1"), value))

    def evaluate(self, o: MarketObservation) -> MarketDecision:
        values = (
            o.input_amount_quote, o.output_amount_base, o.return_amount_quote,
            o.quote_age_ms, o.route_latency_ms, o.gas_cost_quote,
            o.dex_fees_quote, o.flash_fee_quote, o.slippage_quote,
            o.sponsor_cost_quote, o.mev_cost_quote,
        )
        if any(_d(v) < 0 for v in values):
            raise ValueError("market observation contains negative values")
        if o.input_amount_quote <= 0 or o.return_amount_quote <= 0:
            return MarketDecision(Decimal("-Infinity"), Decimal("0"), Decimal("0"),
                                  Decimal("0"), Decimal("0"), Decimal("0"),
                                  Decimal("0"), "invalid_amount")
        if o.execution_window_ms <= 0 or o.spread_survival_ms <= 0:
            return MarketDecision(Decimal("-Infinity"), Decimal("0"), Decimal("0"),
                                  Decimal("0"), Decimal("0"), Decimal("0"),
                                  Decimal("0"), "missing_survival_window")

        gross = o.return_amount_quote - o.input_amount_quote
        deterministic_cost = (
            o.gas_cost_quote + o.dex_fees_quote + o.flash_fee_quote
            + o.slippage_quote + o.sponsor_cost_quote + o.mev_cost_quote
        )

        rpc = self._clamp01(o.rpc_success_rate)
        route = self._clamp01(o.route_success_rate)
        execution_factor = rpc * route

        # Probability-free survival factor: it measures how much of the
        # available execution window remains after observed latency and quote
        # age. This is not a fabricated probability.
        consumed = o.quote_age_ms + o.route_latency_ms
        survival_ratio = self._clamp01(
            (o.spread_survival_ms - consumed) / o.execution_window_ms
        )

        # AMM utilization is an economic warning signal, not a hard rejection.
        utilization = self._clamp01(o.liquidity_utilization)
        liquidity_factor = Decimal("1") - utilization

        # Gas volatility penalizes fragile economics proportionally, while
        # remaining transparent and bounded.
        volatility_factor = Decimal("1") / (
            Decimal("1") + max(Decimal("0"), o.gas_volatility_bps) / Decimal("10000")
        )

        expected_loss = max(Decimal("0"), gross - deterministic_cost) * (
            Decimal("1") - execution_factor * survival_ratio * liquidity_factor * volatility_factor
        )
        economic_value = gross - deterministic_cost - expected_loss

        density = economic_value / o.input_amount_quote
        return MarketDecision(
            executable_economic_value=economic_value,
            gross_profit_quote=gross,
            total_cost_quote=deterministic_cost,
            expected_loss_quote=expected_loss,
            survival_factor=survival_ratio,
            execution_factor=execution_factor,
            capital_efficiency=density,
            reason=(
                f"gross={gross};cost={deterministic_cost};expected_loss={expected_loss};"
                f"survival={survival_ratio};execution={execution_factor};"
                f"liquidity={liquidity_factor};gas_volatility={volatility_factor}"
            ),
        )

    def actionable(self, decision: MarketDecision) -> bool:
        return (
            decision.executable_economic_value >= self.min_profit
            and decision.execution_factor > 0
            and decision.survival_factor > 0
        )
