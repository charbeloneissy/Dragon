from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
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

    def __init__(self, adapter, sources: Iterable[str], min_profit: Decimal = Decimal("0.005"), quote_token_decimals: int = 6, max_quote_latency_ms: Decimal = Decimal("1000"), safety_buffer_quote: Decimal = Decimal("0.001"), flash_loan_enabled: bool = False, flash_loan_fee_bps: Decimal = Decimal("0"), native_to_quote_rate: Decimal = Decimal("0"), min_net_bps: Decimal = Decimal("15")):
        if quote_token_decimals < 0 or quote_token_decimals > 36: raise ValueError("quote_token_decimals must be between 0 and 36")
        if Decimal(min_profit) < Decimal("0.005"): raise ValueError("min_profit cannot be below 0.005")
        if Decimal(max_quote_latency_ms) <= 0: raise ValueError("max_quote_latency_ms must be positive")
        if Decimal(safety_buffer_quote) < 0: raise ValueError("safety_buffer_quote cannot be negative")
        if Decimal(flash_loan_fee_bps) < 0 or Decimal(flash_loan_fee_bps) > 1000: raise ValueError("flash_loan_fee_bps must be between 0 and 1000")
        if not flash_loan_enabled and Decimal(flash_loan_fee_bps) != 0: raise ValueError("flash_loan_fee_bps requires flash_loan_enabled=true")
        if Decimal(native_to_quote_rate) < 0: raise ValueError("native_to_quote_rate cannot be negative")
        if Decimal(min_net_bps) < 0 or Decimal(min_net_bps) > Decimal("10000"): raise ValueError("min_net_bps must be between 0 and 10000")
        self.adapter = adapter
        self.sources = tuple(dict.fromkeys(s.strip() for s in sources if s.strip()))
        self.min_profit = Decimal(min_profit)
        self.quote_token_decimals = quote_token_decimals
        self.max_quote_latency_ms = Decimal(max_quote_latency_ms)
        self.safety_buffer_quote = Decimal(safety_buffer_quote)
        self.flash_loan_enabled = bool(flash_loan_enabled)
        self.flash_loan_fee_bps = Decimal(flash_loan_fee_bps)
        self.native_to_quote_rate = Decimal(native_to_quote_rate)
        self.min_net_bps = Decimal(os.getenv("DEX_MIN_NET_BPS", str(min_net_bps)))
        # Keep isolated RPC/Web3 adapters alive across scans. Rebuilding them per
        # candidate caused repeated chain/factory discovery and added avoidable latency.
        self._quote_adapters: dict[str, object] = {}
        if hasattr(adapter, "clone_for_concurrent_quotes"):
            for source in self.sources:
                try:
                    self._quote_adapters[source] = adapter.clone_for_concurrent_quotes()
                except Exception as exc:
                    logging.warning("DEX adapter clone failed source=%s; falling back to shared adapter: %s", source, exc)
        if not self._quote_adapters:
            self._quote_adapters = {source: adapter for source in self.sources}
        self._isolated_quote_adapters = all(self._quote_adapters.get(source) is not adapter for source in self.sources)
        self.last_rejections: dict[str, int] = {}

    def _reject(self, reason: str) -> None:
        self.last_rejections[reason] = self.last_rejections.get(reason, 0) + 1

    def _quote_latency(self, quote: DexQuote) -> Decimal:
        try:
            value = Decimal(str(getattr(quote, "latency_ms", 0)))
        except Exception:
            return Decimal("Infinity")
        return value if value.is_finite() and value >= 0 else Decimal("Infinity")

    def _quote_quality_ok(self, quote: DexQuote, *, sell_token: str, buy_token: str, sell_amount: int, stage: str) -> bool:
        """Validate quote fields explicitly so real DEX quotes are not hidden behind a generic rejection."""
        try:
            actual_sell = str(getattr(quote, "sell_token", "")).strip()
            actual_buy = str(getattr(quote, "buy_token", "")).strip()
            quoted_in = Decimal(str(getattr(quote, "sell_amount", 0)))
            quoted_out = Decimal(str(getattr(quote, "buy_amount", 0)))
        except (ValueError, TypeError, ArithmeticError) as exc:
            self._reject(f"{stage}_invalid_quote")
            logging.warning(
                "DEX %s quote validation parse failed sell=%s buy=%s amount=%s error=%s: %s",
                stage, sell_token, buy_token, sell_amount, type(exc).__name__, exc,
            )
            return False

        if actual_sell.lower() != sell_token.lower():
            self._reject(f"{stage}_token_mismatch")
            logging.warning("DEX %s quote token mismatch sell expected=%s actual=%s", stage, sell_token, actual_sell)
            return False
        if actual_buy.lower() != buy_token.lower():
            self._reject(f"{stage}_token_mismatch")
            logging.warning("DEX %s quote token mismatch buy expected=%s actual=%s", stage, buy_token, actual_buy)
            return False
        if not quoted_in.is_finite() or not quoted_out.is_finite() or quoted_in <= 0 or quoted_out <= 0:
            self._reject(f"{stage}_invalid_amount")
            return False
        if quoted_in != Decimal(int(sell_amount)):
            self._reject(f"{stage}_amount_mismatch")
            logging.warning("DEX %s quote amount mismatch expected=%s actual=%s", stage, sell_amount, quoted_in)
            return False

        # Raw ERC-20 unit ratios are invalid across tokens with different decimals.
        # Optional bounds are expressed in human units and remain disabled by default.
        min_ratio_raw = os.getenv("DEX_MIN_QUOTE_PRICE_RATIO", "").strip()
        max_ratio_raw = os.getenv("DEX_MAX_QUOTE_PRICE_RATIO", "").strip()
        if min_ratio_raw or max_ratio_raw:
            try:
                sell_decimals = int(os.getenv("DEX_SELL_TOKEN_DECIMALS", str(self.quote_token_decimals)))
                buy_decimals = int(os.getenv("DEX_BUY_TOKEN_DECIMALS", str(self.quote_token_decimals)))
                if not 0 <= sell_decimals <= 36 or not 0 <= buy_decimals <= 36:
                    raise ValueError("price decimals out of range")
                human_in = quoted_in / (Decimal(10) ** sell_decimals)
                human_out = quoted_out / (Decimal(10) ** buy_decimals)
                ratio = human_out / human_in
                if min_ratio_raw:
                    min_ratio = Decimal(min_ratio_raw)
                    if not min_ratio.is_finite() or min_ratio <= 0 or ratio < min_ratio:
                        self._reject(f"{stage}_quote_price_bound")
                        return False
                if max_ratio_raw:
                    max_ratio = Decimal(max_ratio_raw)
                    if not max_ratio.is_finite() or max_ratio <= 0 or ratio > max_ratio:
                        self._reject(f"{stage}_quote_price_bound")
                        return False
            except (ValueError, TypeError, ArithmeticError) as exc:
                self._reject(f"{stage}_invalid_price_bound")
                logging.warning("DEX %s price-bound validation failed error=%s: %s", stage, type(exc).__name__, exc)
                return False
        return True

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
        return (gas_native / Decimal(10) ** 18) * native_to_quote_rate

    def _candidate_amounts(self, ceiling: int) -> list[int]:
        """Adaptive deterministic size grid with denser coverage near both ends."""
        if ceiling <= 0: return []
        configured = int(os.getenv("DEX_MAX_QUOTE_CANDIDATES", "12"))
        max_candidates = max(4, min(32, configured))
        if max_candidates == 1: return [ceiling]
        # Always include both a small probe and the full flash-liquidity ceiling;
        # the old fixed prefix stopped at 60% when candidates were reduced to 8.
        fractions = [
            Decimal("0.05"), Decimal("0.10"), Decimal("0.20"), Decimal("0.30"),
            Decimal("0.40"), Decimal("0.50"), Decimal("0.75"), Decimal("1.00"),
        ]
        # Keep the grid representative when the candidate budget is small:
        # always probe both low size and full available size instead of truncating
        # the fraction list at 30%, which can miss the most profitable size.
        if max_candidates <= len(fractions):
            if max_candidates == 4:
                selected = [Decimal("0.05"), Decimal("0.20"), Decimal("0.50"), Decimal("1.00")]
            else:
                idx = [round(i * (len(fractions) - 1) / (max_candidates - 1)) for i in range(max_candidates)]
                selected = [fractions[i] for i in sorted(set(idx))]
                while len(selected) < max_candidates:
                    selected.append(fractions[len(selected)])
        else:
            selected = fractions[:]
        amounts = [max(1, int(Decimal(ceiling) * f)) for f in selected]
        if max_candidates > len(fractions):
            for i in range(len(fractions) + 1, max_candidates + 1):
                amounts.append(max(1, (ceiling * i) // max_candidates))
        return sorted(set(amounts))

    def scan_max_profitable(self, *, chain_id: int, quote_token: str, base_token: str, max_quote_amount: Decimal, taker: str, slippage_bps: int = 50) -> list[DexOpportunity]:
        if max_quote_amount <= 0: raise ValueError("max_quote_amount must be positive")
        scale = Decimal(10) ** self.quote_token_decimals; ceiling = int(max_quote_amount * scale)
        if ceiling <= 0: return []
        self.last_rejections = {}; profitable: list[DexOpportunity] = []
        candidates = self._candidate_amounts(ceiling)

        # Candidate sizes are independent. Evaluate them concurrently so the scan
        # latency is bounded by the slowest candidate rather than the sum of all
        # candidate RPC round trips. Keep the worker count bounded for RPC safety.
        max_workers = max(1, min(len(candidates), int(os.getenv("DEX_CANDIDATE_CONCURRENCY", "4"))))
        def _scan(candidate: int):
            return self.scan_once(
                chain_id=chain_id, quote_token=quote_token, base_token=base_token,
                quote_amount=candidate, taker=taker, slippage_bps=slippage_bps,
            )
        if max_workers == 1:
            results = [_scan(candidate) for candidate in candidates]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_scan, candidates))
        for result in results:
            profitable.extend(result)
        return [max(profitable, key=lambda x: (x.net_profit_quote, x.quote_amount))] if profitable else []

    def scan_once(self, *, chain_id: int, quote_token: str, base_token: str, quote_amount: int, taker: str, slippage_bps: int = 50) -> list[DexOpportunity]:
        if len(self.sources) < 2: raise ValueError("DEX cross-exchange mode requires at least two DEX sources")
        if quote_amount <= 0: raise ValueError("quote_amount must be positive")
        if not 0 <= slippage_bps <= 5000: raise ValueError("slippage_bps must be between 0 and 5000")
        quote_adapters = self._quote_adapters
        buy_quotes: dict[str, tuple[DexQuote, DexExecution]] = {}
        sell_sources: set[str] = set()
        opportunities: list[DexOpportunity] = []

        def _buy(source):
            return source, quote_adapters.get(source, self.adapter).quote_single_source(
                chain_id=chain_id, sell_token=quote_token, buy_token=base_token,
                sell_amount=quote_amount, taker=taker, source=source, slippage_bps=slippage_bps,
            )

        # All venue quotes are independent. Run them concurrently with a bounded worker pool
        # so adding venues improves coverage without creating unbounded threads.
        max_quote_workers = max(2, min(len(self.sources), int(os.getenv("DEX_QUOTE_CONCURRENCY", "8"))))
        with ThreadPoolExecutor(max_workers=max_quote_workers) as pool:
            futures = [pool.submit(_buy, source) for source in self.sources]
            for future in as_completed(futures):
                source = "unknown"
                try:
                    source, (quote, execution) = future.result()
                except Exception as exc:
                    logging.warning("DEX buy quote failed source=%s sell=%s buy=%s amount=%s error=%s: %s", source, quote_token, base_token, quote_amount, type(exc).__name__, exc)
                    self._reject("buy_quote_error"); continue
                quote_latency = self._quote_latency(quote)
                if quote_latency > self.max_quote_latency_ms:
                    # latency_ms measures request duration, not quote age. Keep the
                    # hard safety gate, but report the real reason so telemetry does
                    # not falsely label a slow fresh quote as "stale".
                    self._reject("buy_quote_slow")
                    logging.info(
                        "DEX buy quote rejected latency_ms=%.1f limit_ms=%s source=%s amount=%s",
                        quote_latency, self.max_quote_latency_ms, source, quote_amount,
                    )
                    continue
                if not self._quote_quality_ok(quote, sell_token=quote_token, buy_token=base_token, sell_amount=quote_amount, stage="buy"):
                    continue
                if execution.buy_amount <= 0:
                    self._reject("buy_zero_output"); continue
                buy_quotes[source] = (quote, execution)

        sell_jobs = []
        for buy_source, (buy_quote, buy_execution) in buy_quotes.items():
            bought_amount = buy_execution.buy_amount
            for source in self.sources:
                if source == buy_source:
                    continue
                sell_jobs.append((buy_source, buy_quote, buy_execution, source, bought_amount))

        def _sell(job):
            buy_source, buy_quote, buy_execution, source, bought_amount = job
            sell_adapter = quote_adapters.get(source, self.adapter)
            return buy_source, buy_quote, buy_execution, source, sell_adapter.quote_single_source(
                chain_id=chain_id, sell_token=base_token, buy_token=quote_token,
                sell_amount=bought_amount, taker=taker, source=source, slippage_bps=slippage_bps,
            )

        def _process_sell(job, result):
            buy_source, buy_quote, buy_execution, source, (sell_quote, sell_execution) = result
            sell_latency = self._quote_latency(sell_quote)
            if sell_latency > self.max_quote_latency_ms:
                self._reject("sell_quote_slow")
                logging.info(
                    "DEX sell quote rejected latency_ms=%.1f limit_ms=%s source=%s amount=%s",
                    sell_latency, self.max_quote_latency_ms, source, buy_execution.buy_amount,
                )
                return
            if not self._quote_quality_ok(sell_quote, sell_token=base_token, buy_token=quote_token, sell_amount=buy_execution.buy_amount, stage="sell"):
                return
            if sell_execution.buy_amount <= 0:
                self._reject("sell_zero_output")
                return
            sell_sources.add(source)

            scale = Decimal(10) ** self.quote_token_decimals
            flash_loan_fee_quote = (
                Decimal(quote_amount) / scale * self.flash_loan_fee_bps / Decimal("10000")
                if self.flash_loan_enabled else Decimal("0")
            )
            native_to_quote_rate = self.native_to_quote_rate
            needs_native_rate = (
                (
                    getattr(sell_quote, "gas_quote", None) is None
                    or Decimal(str(getattr(sell_quote, "gas_quote", 0))) <= 0
                )
                and (
                    Decimal(str(getattr(sell_quote, "gas_native", 0))) > 0
                    or Decimal(str(getattr(sell_execution, "gas", 0)))
                    * Decimal(str(getattr(sell_execution, "gas_price", 0))) > 0
                )
            )
            if native_to_quote_rate <= 0 and needs_native_rate:
                try:
                    rate_adapter = quote_adapters.get(source, self.adapter)
                    raw_rate = Decimal(str(rate_adapter.native_to_quote_rate(
                        chain_id=chain_id,
                        quote_token=quote_token,
                        sell_amount_native=10**15,
                        taker=taker,
                    )))
                    native_to_quote_rate = raw_rate * (Decimal(10) ** 18) / scale
                except Exception:
                    self._reject("native_to_quote_rate_error")
                    return
            if native_to_quote_rate < 0:
                self._reject("negative_native_to_quote_rate")
                return

            # Slippage is transaction protection, not an additional economic haircut.
            buy_slippage_bps = max(
                Decimal(slippage_bps),
                Decimal(str(getattr(buy_quote, "slippage_bps", 0))),
            )
            sell_slippage_bps = max(
                Decimal(slippage_bps),
                Decimal(str(getattr(sell_quote, "slippage_bps", 0))),
            )
            if buy_slippage_bps > 5000 or sell_slippage_bps > 5000:
                self._reject("slippage_too_wide")
                return
            final_amount = int(sell_execution.buy_amount)
            if final_amount <= 0:
                self._reject("slippage_zero_output")
                return

            gas_cost_quote = (
                self._gas_cost_quote(buy_quote, buy_execution, native_to_quote_rate)
                + self._gas_cost_quote(sell_quote, sell_execution, native_to_quote_rate)
            )
            if not gas_cost_quote.is_finite():
                self._reject("gas_unpriced")
                return

            # Explicit two-leg round-trip accounting: quote -> base -> quote.
            # Profit is measured only from the final quote-token amount versus
            # the original quote-token amount, after both legs have been quoted.
            round_trip_return = Decimal(final_amount) / Decimal(quote_amount)
            gross = Decimal(final_amount - quote_amount) / scale
            cost_quote = gas_cost_quote + flash_loan_fee_quote + self.safety_buffer_quote
            notional_quote = Decimal(quote_amount) / scale
            gross_bps = (round_trip_return - Decimal("1")) * Decimal("10000")
            cost_bps = (cost_quote / notional_quote) * Decimal("10000") if notional_quote > 0 else Decimal("0")
            net_bps = gross_bps - cost_bps
            net = gross - cost_quote
            logging.info(
                "DEX ROUND_TRIP buy=%s sell=%s token=%s start_quote_raw=%s leg1_base_raw=%s leg2_quote_raw=%s return=%.8f gross=%s gross_bps=%.3f cost=%s cost_bps=%.3f gas=%s flash_fee=%s safety=%s net=%s net_bps=%.3f",
                buy_source, source, base_token, quote_amount, buy_execution.buy_amount,
                final_amount, round_trip_return, gross, gross_bps, cost_quote, cost_bps,
                gas_cost_quote, flash_loan_fee_quote, self.safety_buffer_quote,
                net, net_bps,
            )
            if not net.is_finite():
                self._reject("nonfinite_net_profit")
                return
            if net < self.min_profit:
                self._reject("net_profit_below_min")
                return
            if net_bps < self.min_net_bps:
                self._reject("net_bps_below_min")
                logging.info(
                    "DEX opportunity rejected net_bps=%.3f min_net_bps=%.3f buy=%s sell=%s amount=%s",
                    net_bps, self.min_net_bps, buy_source, source, quote_amount,
                )
                return

            opportunities.append(DexOpportunity(
                chain_id=chain_id,
                buy_source=buy_source,
                sell_source=source,
                base_token=base_token,
                quote_token=quote_token,
                quote_amount=quote_amount,
                bought_amount=buy_execution.buy_amount,
                final_amount=final_amount,
                gross_profit_quote=gross,
                net_profit_quote=net,
                gas_cost_quote=gas_cost_quote,
                flash_loan_fee_quote=flash_loan_fee_quote,
                safety_buffer_quote=self.safety_buffer_quote,
                first_leg=buy_execution,
                second_leg=sell_execution,
            ))

        # Sell legs are independent across buy/sell venue combinations. With isolated
        # adapters, evaluate them concurrently with a bounded worker pool; otherwise
        # preserve safe sequential behavior.
        if sell_jobs:
            if self._isolated_quote_adapters:
                max_sell_workers = max(2, min(len(sell_jobs), int(os.getenv("DEX_SELL_CONCURRENCY", "8"))))
                with ThreadPoolExecutor(max_workers=max_sell_workers) as pool:
                    futures = {pool.submit(_sell, job): job for job in sell_jobs}
                    for future in as_completed(futures):
                        job = futures[future]
                        try:
                            _process_sell(job, future.result())
                        except Exception as exc:
                            source = job[3]
                            logging.warning(
                                "DEX sell quote failed source=%s sell=%s buy=%s amount=%s error=%s: %s",
                                source, base_token, quote_token, job[4], type(exc).__name__, exc,
                            )
                            self._reject("sell_quote_error")
            else:
                for job in sell_jobs:
                    try:
                        _process_sell(job, _sell(job))
                    except Exception as exc:
                        source = job[3]
                        logging.warning(
                            "DEX sell quote failed source=%s sell=%s buy=%s amount=%s error=%s: %s",
                            source, base_token, quote_token, job[4], type(exc).__name__, exc,
                        )
                        self._reject("sell_quote_error")

        if not buy_quotes: self._reject("no_buy_sources")
        if not sell_sources and buy_quotes: self._reject("no_sell_sources")
        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
