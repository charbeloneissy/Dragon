from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

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
    """Cross-DEX scanner using single-source 0x routes.

    The scanner intentionally rejects mixed-source routes. This preserves the
    Dragon rule that an opportunity must be cross-DEX, not a hidden multi-hop
    or triangular route inside one aggregator quote.
    """

    def __init__(self, adapter: ZeroXAdapter, sources: Iterable[str], min_profit: Decimal = Decimal("0.005")):
        self.adapter = adapter
        self.sources = tuple(dict.fromkeys(s.strip() for s in sources if s.strip()))
        self.min_profit = min_profit

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

        quotes: dict[str, tuple[object, DexExecution]] = {}
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
            quotes[source] = (quote, execution)

        opportunities: list[DexOpportunity] = []
        for buy_source, (_buy_quote, buy_execution) in quotes.items():
            bought_amount = buy_execution.buy_amount
            if bought_amount <= 0:
                continue
            for sell_source in self.sources:
                if sell_source == buy_source:
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
                final_amount = sell_execution.buy_amount
                gross = Decimal(final_amount - quote_amount) / Decimal(10 ** 6)
                gas_quote = sell_quote.gas_quote + _buy_quote.gas_quote
                net = gross - gas_quote
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


def _single_source_guard(route: dict, source: str) -> None:
    fills = route.get("fills") or []
    if not fills:
        raise RuntimeError("0x quote returned no route fills")
    actual_sources = {str(fill.get("source")) for fill in fills}
    if actual_sources != {source}:
        raise RuntimeError(f"route is not single-source: expected {source}, got {sorted(actual_sources)}")
