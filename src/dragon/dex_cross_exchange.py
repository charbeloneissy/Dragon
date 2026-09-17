from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .dex import DexQuote
from .dex_0x import DexExecution, ZeroXAdapter


@dataclass(frozen=True)
class DexOpportunity:
    chain_id: int
    buy_source: str
    sell_source: str
    base_token: str
    quote_token: str
    quote_amount: int
    bought_amount: int
    final_amount: int
    gross_profit_quote: Decimal
    net_profit_quote: Decimal
    gas_cost_quote: Decimal
    flash_loan_fee_quote: Decimal
    safety_buffer_quote: Decimal
    first_leg: DexExecution
    second_leg: DexExecution


class DexCrossExchangeEngine:
    """Two-leg cross-DEX scanner with conservative net-profit accounting."""

    def __init__(
        self,
        adapter: ZeroXAdapter,
        sources: Iterable[str],
        min_profit: Decimal = Decimal("0.005"),
        quote_token_decimals: int = 6,
        max_quote_latency_ms: Decimal = Decimal("1000"),
        safety_buffer_quote: Decimal = Decimal("0.001"),
        flash_loan_enabled: bool = False,
        flash_loan_fee_bps: Decimal = Decimal("0"),
        native_to_quote_rate: Decimal = Decimal("0"),
    ):
        if quote_token_decimals < 0 or quote_token_decimals > 36:
            raise ValueError("quote_token_decimals must be between 0 and 36")
        if Decimal(min_profit) < Decimal("0.005"):
            raise ValueError("min_profit cannot be below 0.005")
        if Decimal(max_quote_latency_ms) <= 0:
            raise ValueError("max_quote_latency_ms must be positive")
        if Decimal(safety_buffer_quote) < 0:
            raise ValueError("safety_buffer_quote cannot be negative")
        if Decimal(flash_loan_fee_bps) < 0 or Decimal(flash_loan_fee_bps) > 1000:
            raise ValueError("flash_loan_fee_bps must be between 0 and 1000")
        if not flash_loan_enabled and Decimal(flash_loan_fee_bps) != 0:
            raise ValueError("flash_loan_fee_bps requires flash_loan_enabled=true")
        if Decimal(native_to_quote_rate) < 0:
            raise ValueError("native_to_quote_rate cannot be negative")

        self.adapter = adapter
        self.sources = tuple(dict.fromkeys(s.strip() for s in sources if s.strip()))
        self.min_profit = Decimal(min_profit)
        self.quote_token_decimals = quote_token_decimals
        self.max_quote_latency_ms = Decimal(max_quote_latency_ms)
        self.safety_buffer_quote = Decimal(safety_buffer_quote)
        self.flash_loan_enabled = bool(flash_loan_enabled)
        self.flash_loan_fee_bps = Decimal(flash_loan_fee_bps)
        self.native_to_quote_rate = Decimal(native_to_quote_rate)

    def _quote_latency(self, quote: DexQuote) -> Decimal:
        return Decimal(str(getattr(quote, "latency_ms", 0)))

    def _gas_cost_quote(
        self,
        quote: DexQuote,
        execution: DexExecution,
        native_to_quote_rate: Decimal,
    ) -> Decimal:
        """Convert execution gas to quote-token units without treating zero as free.

        A positive explicit gas_quote is authoritative. Otherwise gas_native is
        used; if it is absent, gas * gas_price is treated as native base units.
        A missing native-to-quote rate makes the candidate non-executable rather
        than silently assigning zero gas cost.
        """
        direct = getattr(quote, "gas_quote", None)
        if direct is not None:
            direct_quote = Decimal(str(direct))
            if direct_quote > 0:
                return direct_quote

        gas_native = getattr(quote, "gas_native", None)
        if gas_native is None:
            gas_native = Decimal(str(getattr(execution, "gas", 0))) * Decimal(
                str(getattr(execution, "gas_price", 0))
            )
        gas_native = Decimal(str(gas_native))
        if gas_native <= 0:
            return Decimal("0")
        if native_to_quote_rate <= 0:
            return Decimal("Infinity")
        return (gas_native / Decimal(10) ** 18) * native_to_quote_rate

    def _candidate_amounts(self, ceiling: int) -> list[int]:
        """Return logarithmic sizing probes plus the exact ceiling.

        Profit is not assumed to be monotonic with size: price impact can make
        a larger trade less profitable. The old binary-search logic therefore
        could move in the wrong direction and select a suboptimal size.
        """
        if ceiling <= 0:
            return []
        amounts = {1, ceiling}
        points = 64
        for i in range(1, points):
            # Integer geometric spacing without floating-point arithmetic.
            amount = max(1, (ceiling * i) // points)
            amounts.add(amount)
        # Also preserve powers of two because they are useful around small
        # account sizes and minimum order boundaries.
        amount = 1
        while amount < ceiling:
            amounts.add(amount)
            amount *= 2
        return sorted(amounts)

    def scan_max_profitable(
        self,
        *,
        chain_id: int,
        quote_token: str,
        base_token: str,
        max_quote_amount: Decimal,
        taker: str,
        slippage_bps: int = 50,
    ) -> list[DexOpportunity]:
        if max_quote_amount <= 0:
            raise ValueError("max_quote_amount must be positive")

        scale = Decimal(10) ** self.quote_token_decimals
        ceiling = int(max_quote_amount * scale)
        if ceiling <= 0:
            return []

        profitable: list[DexOpportunity] = []
        for candidate in self._candidate_amounts(ceiling):
            profitable.extend(
                self.scan_once(
                    chain_id=chain_id,
                    quote_token=quote_token,
                    base_token=base_token,
                    quote_amount=candidate,
                    taker=taker,
                    slippage_bps=slippage_bps,
                )
            )

        if not profitable:
            return []

        # Optimize the quantity that actually matters: net profit after gas,
        # flash-loan fee and safety buffer. Never optimize merely for notional.
        best = max(
            profitable,
            key=lambda x: (x.net_profit_quote, x.quote_amount),
        )
        return [best]

    def scan_once(
        self,
        *,
        chain_id: int,
        quote_token: str,
        base_token: str,
        quote_amount: int,
        taker: str,
        slippage_bps: int = 50,
    ) -> list[DexOpportunity]:
        if len(self.sources) < 2:
            raise ValueError("DEX cross-exchange mode requires at least two DEX sources")
        if quote_amount <= 0:
            raise ValueError("quote_amount must be positive")
        if not (0 <= slippage_bps <= 5000):
            raise ValueError("slippage_bps must be between 0 and 5000")

        quotes: dict[str, tuple[DexQuote, DexExecution]] = {}
        for source in self.sources:
            try:
                quote, execution = self.adapter.quote_single_source(
                    chain_id=chain_id,
                    sell_token=quote_token,
                    buy_token=base_token,
                    sell_amount=quote_amount,
                    taker=taker,
                    source=source,
                    slippage_bps=slippage_bps,
                )
            except Exception:
                continue
            if self._quote_latency(quote) > self.max_quote_latency_ms:
                continue
            if execution.buy_amount <= 0:
                continue
            quotes[source] = (quote, execution)

        if len(quotes) < 2:
            return []

        opportunities: list[DexOpportunity] = []
        scale = Decimal(10) ** self.quote_token_decimals
        flash_loan_fee_quote = (
            Decimal(quote_amount) / scale * self.flash_loan_fee_bps / Decimal("10000")
            if self.flash_loan_enabled
            else Decimal("0")
        )

        native_to_quote_rate = self.native_to_quote_rate
        needs_native_rate = any(
            (
                getattr(q, "gas_quote", None) is None
                or Decimal(str(getattr(q, "gas_quote", 0))) <= 0
            )
            and (
                Decimal(str(getattr(q, "gas_native", 0))) > 0
                or Decimal(str(getattr(e, "gas", 0))) * Decimal(str(getattr(e, "gas_price", 0))) > 0
            )
            for q, e in quotes.values()
        )
        if native_to_quote_rate <= 0 and needs_native_rate:
            try:
                native_to_quote_rate = Decimal(
                    str(
                        self.adapter.native_to_quote_rate(
                            chain_id=chain_id,
                            quote_token=quote_token,
                            sell_amount_native=10**15,
                            taker=taker,
                        )
                    )
                )
            except Exception:
                return []
        if native_to_quote_rate < 0:
            return []

        for buy_source, (buy_quote, buy_execution) in quotes.items():
            bought_amount = buy_execution.buy_amount
            if bought_amount <= 0:
                continue

            for sell_source in quotes:
                if sell_source == buy_source:
                    continue
                try:
                    sell_quote, sell_execution = self.adapter.quote_single_source(
                        chain_id=chain_id,
                        sell_token=base_token,
                        buy_token=quote_token,
                        sell_amount=bought_amount,
                        taker=taker,
                        source=sell_source,
                        slippage_bps=slippage_bps,
                    )
                except Exception:
                    continue
                if self._quote_latency(sell_quote) > self.max_quote_latency_ms:
                    continue

                final_amount = sell_execution.buy_amount
                if final_amount <= 0:
                    continue

                gas_cost_quote = self._gas_cost_quote(
                    buy_quote, buy_execution, native_to_quote_rate
                ) + self._gas_cost_quote(
                    sell_quote, sell_execution, native_to_quote_rate
                )
                if not gas_cost_quote.is_finite():
                    continue

                gross = Decimal(final_amount - quote_amount) / scale
                net = gross - gas_cost_quote - flash_loan_fee_quote - self.safety_buffer_quote

                # Only candidates that remain profitable after every modeled
                # cost can enter the execution pipeline.
                if net < self.min_profit:
                    continue

                opportunities.append(
                    DexOpportunity(
                        chain_id=chain_id,
                        buy_source=buy_source,
                        sell_source=sell_source,
                        base_token=base_token,
                        quote_token=quote_token,
                        quote_amount=quote_amount,
                        bought_amount=bought_amount,
                        final_amount=final_amount,
                        gross_profit_quote=gross,
                        net_profit_quote=net,
                        gas_cost_quote=gas_cost_quote,
                        flash_loan_fee_quote=flash_loan_fee_quote,
                        safety_buffer_quote=self.safety_buffer_quote,
                        first_leg=buy_execution,
                        second_leg=sell_execution,
                    )
                )

        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
