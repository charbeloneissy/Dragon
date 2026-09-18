from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .dex import DexQuote
from .dex_0x import DexExecution


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

    def __init__(self, adapter, sources: Iterable[str], min_profit: Decimal = Decimal("0.005"), quote_token_decimals: int = 6, max_quote_latency_ms: Decimal = Decimal("1000"), safety_buffer_quote: Decimal = Decimal("0.001"), flash_loan_enabled: bool = False, flash_loan_fee_bps: Decimal = Decimal("0"), native_to_quote_rate: Decimal = Decimal("0")):
        if quote_token_decimals < 0 or quote_token_decimals > 36: raise ValueError("quote_token_decimals must be between 0 and 36")
        if Decimal(min_profit) < Decimal("0.005"): raise ValueError("min_profit cannot be below 0.005")
        if Decimal(max_quote_latency_ms) <= 0: raise ValueError("max_quote_latency_ms must be positive")
        if Decimal(safety_buffer_quote) < 0: raise ValueError("safety_buffer_quote cannot be negative")
        if Decimal(flash_loan_fee_bps) < 0 or Decimal(flash_loan_fee_bps) > 1000: raise ValueError("flash_loan_fee_bps must be between 0 and 1000")
        if not flash_loan_enabled and Decimal(flash_loan_fee_bps) != 0: raise ValueError("flash_loan_fee_bps requires flash_loan_enabled=true")
        if Decimal(native_to_quote_rate) < 0: raise ValueError("native_to_quote_rate cannot be negative")
        self.adapter = adapter
        self.sources = tuple(dict.fromkeys(s.strip() for s in sources if s.strip()))
        self.min_profit = Decimal(min_profit)
        self.quote_token_decimals = quote_token_decimals
        self.max_quote_latency_ms = Decimal(max_quote_latency_ms)
        self.safety_buffer_quote = Decimal(safety_buffer_quote)
        self.flash_loan_enabled = bool(flash_loan_enabled)
        self.flash_loan_fee_bps = Decimal(flash_loan_fee_bps)
        self.native_to_quote_rate = Decimal(native_to_quote_rate)
        self.last_rejections: dict[str, int] = {}

    def _reject(self, reason: str) -> None:
        self.last_rejections[reason] = self.last_rejections.get(reason, 0) + 1

    def _quote_latency(self, quote: DexQuote) -> Decimal:
        return Decimal(str(getattr(quote, "latency_ms", 0)))

    def _gas_cost_quote(self, quote: DexQuote, execution: DexExecution, native_to_quote_rate: Decimal) -> Decimal:
        direct = getattr(quote, "gas_quote", None)
        if direct is not None:
            direct_quote = Decimal(str(direct))
            if direct_quote > 0:
                return direct_quote
        gas_native = getattr(quote, "gas_native", None)
        if gas_native is None:
            gas_native = Decimal(str(getattr(execution, "gas", 0))) * Decimal(str(getattr(execution, "gas_price", 0)))
        gas_native = Decimal(str(gas_native))
        if gas_native <= 0:
            return Decimal("0")
        if native_to_quote_rate <= 0:
            return Decimal("Infinity")
        # native_to_quote_rate is normalized to quote tokens per 1 native token.
        return (gas_native / Decimal(10) ** 18) * native_to_quote_rate

    def _candidate_amounts(self, ceiling: int) -> list[int]:
        """Return a small deterministic size grid; avoid quote-spam sweeps."""
        if ceiling <= 0: return []
        max_candidates = max(1, min(16, int(os.getenv("DEX_MAX_QUOTE_CANDIDATES", "4"))))
        if max_candidates == 1: return [ceiling]
        if max_candidates == 2: return [max(1, ceiling // 2), ceiling]
        amounts = []
        for i in range(1, max_candidates + 1):
            amounts.append(max(1, (ceiling * i) // max_candidates))
        return sorted(set(amounts))

    def scan_max_profitable(self, *, chain_id: int, quote_token: str, base_token: str, max_quote_amount: Decimal, taker: str, slippage_bps: int = 50) -> list[DexOpportunity]:
        if max_quote_amount <= 0: raise ValueError("max_quote_amount must be positive")
        scale = Decimal(10) ** self.quote_token_decimals; ceiling = int(max_quote_amount * scale)
        if ceiling <= 0: return []
        self.last_rejections = {}; profitable: list[DexOpportunity] = []
        for candidate in self._candidate_amounts(ceiling):
            profitable.extend(self.scan_once(chain_id=chain_id, quote_token=quote_token, base_token=base_token, quote_amount=candidate, taker=taker, slippage_bps=slippage_bps))
        return [max(profitable, key=lambda x: (x.net_profit_quote, x.quote_amount))] if profitable else []

    def scan_once(self, *, chain_id: int, quote_token: str, base_token: str, quote_amount: int, taker: str, slippage_bps: int = 50) -> list[DexOpportunity]:
        if len(self.sources) < 2: raise ValueError("DEX cross-exchange mode requires at least two DEX sources")
        if quote_amount <= 0: raise ValueError("quote_amount must be positive")
        if not 0 <= slippage_bps <= 5000: raise ValueError("slippage_bps must be between 0 and 5000")
        buy_quotes: dict[str, tuple[DexQuote, DexExecution]] = {}
        sell_sources: set[str] = set()
        opportunities: list[DexOpportunity] = []

        # Keep buy and sell discovery independent. A DEX that cannot quote the
        # first direction must not prevent it from being tested as the sell leg.
        for source in self.sources:
            try:
                quote, execution = self.adapter.quote_single_source(
                    chain_id=chain_id, sell_token=quote_token, buy_token=base_token,
                    sell_amount=quote_amount, taker=taker, source=source,
                    slippage_bps=slippage_bps,
                )
            except Exception as exc:
                logging.warning(
                    "DEX buy quote failed source=%s sell=%s buy=%s amount=%s error=%s: %s",
                    source, quote_token, base_token, quote_amount, type(exc).__name__, exc,
                )
                self._reject("buy_quote_error")
                continue
            if self._quote_latency(quote) > self.max_quote_latency_ms:
                self._reject("buy_quote_stale"); continue
            if execution.buy_amount <= 0:
                self._reject("buy_zero_output"); continue
            buy_quotes[source] = (quote, execution)

        # Probe the sell direction separately for the amount produced by each
        # successful buy. This allows asymmetric liquidity across DEXes.
        for buy_source, (_, buy_execution) in buy_quotes.items():
            bought_amount = buy_execution.buy_amount
            for source in self.sources:
                if source == buy_source:
                    continue
                try:
                    sell_quote, sell_execution = self.adapter.quote_single_source(
                        chain_id=chain_id, sell_token=base_token, buy_token=quote_token,
                        sell_amount=bought_amount, taker=taker, source=source,
                        slippage_bps=slippage_bps,
                    )
                except Exception as exc:
                    logging.warning(
                        "DEX sell quote failed source=%s sell=%s buy=%s amount=%s error=%s: %s",
                        source, base_token, quote_token, bought_amount,
                        type(exc).__name__, exc,
                    )
                    self._reject("sell_quote_error")
                    continue
                if self._quote_latency(sell_quote) > self.max_quote_latency_ms:
                    self._reject("sell_quote_stale"); continue
                if sell_execution.buy_amount <= 0:
                    self._reject("sell_zero_output"); continue
                sell_sources.add(source)

                scale = Decimal(10) ** self.quote_token_decimals
                flash_loan_fee_quote = Decimal(quote_amount) / scale * self.flash_loan_fee_bps / Decimal("10000") if self.flash_loan_enabled else Decimal("0")
                native_to_quote_rate = self.native_to_quote_rate
                needs_native_rate = (
                    (getattr(sell_quote, "gas_quote", None) is None or Decimal(str(getattr(sell_quote, "gas_quote", 0))) <= 0)
                    and (Decimal(str(getattr(sell_quote, "gas_native", 0))) > 0 or Decimal(str(getattr(sell_execution, "gas", 0))) * Decimal(str(getattr(sell_execution, "gas_price", 0))) > 0)
                )
                if native_to_quote_rate <= 0 and needs_native_rate:
                    try:
                        raw_rate = Decimal(str(self.adapter.native_to_quote_rate(
                            chain_id=chain_id, quote_token=quote_token,
                            sell_amount_native=10**15, taker=taker
                        )))
                        native_to_quote_rate = raw_rate * (Decimal(10) ** 18) / scale
                    except Exception:
                        self._reject("native_to_quote_rate_error"); continue

                if native_to_quote_rate < 0:
                    self._reject("negative_native_to_quote_rate"); continue

                raw_final_amount = sell_execution.buy_amount
                buy_slippage_bps = max(Decimal(slippage_bps), Decimal(str(getattr(buy_quotes[buy_source][0], "slippage_bps", 0))))
                sell_slippage_bps = max(Decimal(slippage_bps), Decimal(str(getattr(sell_quote, "slippage_bps", 0))))
                if buy_slippage_bps > 5000 or sell_slippage_bps > 5000:
                    self._reject("slippage_too_wide"); continue
                buy_slippage_factor = (Decimal(10000) - buy_slippage_bps) / Decimal(10000)
                sell_slippage_factor = (Decimal(10000) - sell_slippage_bps) / Decimal(10000)
                conservative_bought_amount = int(Decimal(bought_amount) * buy_slippage_factor)
                if conservative_bought_amount <= 0:
                    self._reject("slippage_zero_intermediate"); continue
                try:
                    if conservative_bought_amount != bought_amount:
                        sell_quote_conservative, sell_execution_conservative = self.adapter.quote_single_source(
                            chain_id=chain_id, sell_token=base_token, buy_token=quote_token,
                            sell_amount=conservative_bought_amount, taker=taker, source=source,
                            slippage_bps=slippage_bps,
                        )
                        if self._quote_latency(sell_quote_conservative) > self.max_quote_latency_ms:
                            self._reject("sell_quote_stale"); continue
                        raw_final_amount = sell_execution_conservative.buy_amount
                        sell_quote = sell_quote_conservative
                        sell_execution = sell_execution_conservative
                except Exception:
                    self._reject("conservative_sell_quote_error"); continue
                final_amount = int(Decimal(raw_final_amount) * sell_slippage_factor)
                if final_amount <= 0:
                    self._reject("slippage_zero_output"); continue
                gas_cost_quote = self._gas_cost_quote(buy_quotes[buy_source][0], buy_quotes[buy_source][1], native_to_quote_rate) + self._gas_cost_quote(sell_quote, sell_execution, native_to_quote_rate)
                if not gas_cost_quote.is_finite():
                    self._reject("gas_unpriced"); continue
                gross = Decimal(final_amount - quote_amount) / scale
                net = gross - gas_cost_quote - flash_loan_fee_quote - self.safety_buffer_quote
                logging.info(
                    "DEX calc buy=%s sell=%s token=%s amount=%s bought=%s conservative_bought=%s final=%s gross=%s gas=%s flash_fee=%s safety=%s net=%s",
                    buy_source, source, base_token, quote_amount, bought_amount,
                    conservative_bought_amount, final_amount, gross, gas_cost_quote,
                    flash_loan_fee_quote, self.safety_buffer_quote, net,
                )
                if not net.is_finite():
                    self._reject("nonfinite_net_profit"); continue
                if net < self.min_profit:
                    self._reject("net_profit_below_min"); continue
                opportunities.append(DexOpportunity(
                    chain_id=chain_id, buy_source=buy_source, sell_source=source,
                    base_token=base_token, quote_token=quote_token,
                    quote_amount=quote_amount, bought_amount=bought_amount,
                    final_amount=final_amount, gross_profit_quote=gross,
                    net_profit_quote=net, gas_cost_quote=gas_cost_quote,
                    flash_loan_fee_quote=flash_loan_fee_quote,
                    safety_buffer_quote=self.safety_buffer_quote,
                    first_leg=buy_quotes[buy_source][1], second_leg=sell_execution,
                ))

        if not buy_quotes:
            self._reject("no_buy_sources")
        if not sell_sources and buy_quotes:
            self._reject("no_sell_sources")
        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
        scale = Decimal(10) ** self.quote_token_decimals
        flash_loan_fee_quote = Decimal(quote_amount) / scale * self.flash_loan_fee_bps / Decimal("10000") if self.flash_loan_enabled else Decimal("0")
        native_to_quote_rate = self.native_to_quote_rate
        needs_native_rate = any(((getattr(q, "gas_quote", None) is None or Decimal(str(getattr(q, "gas_quote", 0))) <= 0) and (Decimal(str(getattr(q, "gas_native", 0))) > 0 or Decimal(str(getattr(e, "gas", 0))) * Decimal(str(getattr(e, "gas_price", 0))) > 0)) for q, e in quotes.values())
        if native_to_quote_rate <= 0 and needs_native_rate:
            try:
                raw_rate = Decimal(str(self.adapter.native_to_quote_rate(chain_id=chain_id, quote_token=quote_token, sell_amount_native=10**15, taker=taker)))
                # The adapter returns quote base-units per native base-unit. Normalize
                # once here so gas accounting is expressed in human quote units.
                native_to_quote_rate = raw_rate * (Decimal(10) ** 18) / scale
            except Exception: self._reject("native_to_quote_rate_error"); return []
        if native_to_quote_rate < 0: self._reject("negative_native_to_quote_rate"); return []
        for buy_source, (buy_quote, buy_execution) in quotes.items():
            bought_amount = buy_execution.buy_amount
            for sell_source in quotes:
                if sell_source == buy_source: continue
                try: sell_quote, sell_execution = self.adapter.quote_single_source(chain_id=chain_id, sell_token=base_token, buy_token=quote_token, sell_amount=bought_amount, taker=taker, source=sell_source, slippage_bps=slippage_bps)
                except Exception: self._reject("sell_quote_error"); continue
                if self._quote_latency(sell_quote) > self.max_quote_latency_ms: self._reject("sell_quote_stale"); continue
                raw_final_amount = sell_execution.buy_amount
                if raw_final_amount <= 0: self._reject("sell_zero_output"); continue
                # Profitability must survive the same slippage bound used by both executable legs.
                # Apply the slippage haircut to each leg for conservative pre-trade accounting;
                # calldata still enforces the exact minOut on-chain.
                buy_slippage_bps = max(Decimal(slippage_bps), Decimal(str(getattr(buy_quote, "slippage_bps", 0))))
                sell_slippage_bps = max(Decimal(slippage_bps), Decimal(str(getattr(sell_quote, "slippage_bps", 0))))
                if buy_slippage_bps > 5000 or sell_slippage_bps > 5000:
                    self._reject("slippage_too_wide")
                    continue
                buy_slippage_factor = (Decimal(10000) - buy_slippage_bps) / Decimal(10000)
                sell_slippage_factor = (Decimal(10000) - sell_slippage_bps) / Decimal(10000)
                conservative_bought_amount = int(Decimal(bought_amount) * buy_slippage_factor)
                if conservative_bought_amount <= 0: self._reject("slippage_zero_intermediate"); continue
                try:
                    if conservative_bought_amount != bought_amount:
                        sell_quote_conservative, sell_execution_conservative = self.adapter.quote_single_source(chain_id=chain_id, sell_token=base_token, buy_token=quote_token, sell_amount=conservative_bought_amount, taker=taker, source=sell_source, slippage_bps=slippage_bps)
                        if self._quote_latency(sell_quote_conservative) > self.max_quote_latency_ms: self._reject("sell_quote_stale"); continue
                        raw_final_amount = sell_execution_conservative.buy_amount
                        sell_quote = sell_quote_conservative
                        sell_execution = sell_execution_conservative
                except Exception: self._reject("conservative_sell_quote_error"); continue
                final_amount = int(Decimal(raw_final_amount) * sell_slippage_factor)
                if final_amount <= 0: self._reject("slippage_zero_output"); continue
                gas_cost_quote = self._gas_cost_quote(buy_quote, buy_execution, native_to_quote_rate) + self._gas_cost_quote(sell_quote, sell_execution, native_to_quote_rate)
                if not gas_cost_quote.is_finite(): self._reject("gas_unpriced"); continue
                gross = Decimal(final_amount - quote_amount) / scale
                net = gross - gas_cost_quote - flash_loan_fee_quote - self.safety_buffer_quote
                logging.info(
                    "DEX calc buy=%s sell=%s token=%s amount=%s bought=%s conservative_bought=%s final=%s gross=%s gas=%s flash_fee=%s safety=%s net=%s",
                    buy_source, sell_source, base_token, quote_amount, bought_amount,
                    conservative_bought_amount, final_amount, gross, gas_cost_quote,
                    flash_loan_fee_quote, self.safety_buffer_quote, net,
                )
                if not net.is_finite(): self._reject("nonfinite_net_profit"); continue
                if net < self.min_profit: self._reject("net_profit_below_min"); continue
                opportunities.append(DexOpportunity(chain_id=chain_id, buy_source=buy_source, sell_source=sell_source, base_token=base_token, quote_token=quote_token, quote_amount=quote_amount, bought_amount=bought_amount, final_amount=final_amount, gross_profit_quote=gross, net_profit_quote=net, gas_cost_quote=gas_cost_quote, flash_loan_fee_quote=flash_loan_fee_quote, safety_buffer_quote=self.safety_buffer_quote, first_leg=buy_execution, second_leg=sell_execution))
        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
