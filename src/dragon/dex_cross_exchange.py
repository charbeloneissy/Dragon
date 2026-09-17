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
    first_leg: DexExecution
    second_leg: DexExecution


class DexCrossExchangeEngine:
    """Cross-DEX scanner using independently constrained 0x liquidity sources.

    The engine only accepts opportunities where the first and second legs use
    different DEX sources. It does not execute transactions.
    """

    def __init__(
        self,
        adapter: ZeroXAdapter,
        sources: Iterable[str],
        min_profit: Decimal = Decimal("0.005"),
        quote_token_decimals: int = 6,
        max_quote_latency_ms: Decimal = Decimal("1000"),
    ):
        if quote_token_decimals < 0 or quote_token_decimals > 36:
            raise ValueError("quote_token_decimals must be between 0 and 36")
        self.adapter = adapter
        self.sources = tuple(dict.fromkeys(s.strip() for s in sources if s.strip()))
        self.min_profit = Decimal(min_profit)
        self.quote_token_decimals = quote_token_decimals
        self.max_quote_latency_ms = Decimal(max_quote_latency_ms)

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
            quote, execution = self.adapter.quote_single_source(
                chain_id=chain_id,
                sell_token=quote_token,
                buy_token=base_token,
                sell_amount=quote_amount,
                taker=taker,
                source=source,
                slippage_bps=slippage_bps,
            )
            if quote.latency_ms > self.max_quote_latency_ms:
                continue
            if execution.buy_amount <= 0:
                continue
            quotes[source] = (quote, execution)

        opportunities: list[DexOpportunity] = []
        scale = Decimal(10) ** self.quote_token_decimals

        for buy_source, (buy_quote, buy_execution) in quotes.items():
            bought_amount = buy_execution.buy_amount
            for sell_source in self.sources:
                if sell_source == buy_source or sell_source not in quotes and len(quotes) < 2:
                    continue

                sell_quote, sell_execution = self.adapter.quote_single_source(
                    chain_id=chain_id,
                    sell_token=base_token,
                    buy_token=quote_token,
                    sell_amount=bought_amount,
                    taker=taker,
                    source=sell_source,
                    slippage_bps=slippage_bps,
                )
                if sell_quote.latency_ms > self.max_quote_latency_ms:
                    continue

                final_amount = sell_execution.buy_amount
                if final_amount <= 0:
                    continue

                # gas_quote is already represented by the adapter in quote-token
                # units. Never hard-code 1e6 for every possible quote token.
                second_rate = Decimal(final_amount) / Decimal(bought_amount)
                gas_quote_units = buy_quote.gas_quote + (sell_quote.gas_quote * second_rate)
                gross_units = Decimal(final_amount - quote_amount)
                net_units = gross_units - gas_quote_units
                gross = gross_units / scale
                net = net_units / scale

                if net >= self.min_profit:
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
                            first_leg=buy_execution,
                            second_leg=sell_execution,
                        )
                    )

        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
