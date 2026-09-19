from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
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
    compound_amount: int = 0

    @property
    def flash_loan_amount(self) -> int:
        return self.quote_amount - self.compound_amount


class DexCrossExchangeEngine:
    """Two-leg cross-DEX scanner with conservative net-profit accounting."""

    def __init__(self, adapter, sources: Iterable[str], min_profit: Decimal = Decimal("0.005"), quote_token_decimals: int = 6, max_quote_latency_ms: Decimal = Decimal("500"), safety_buffer_quote: Decimal = Decimal("0.001"), flash_loan_enabled: bool = False, flash_loan_fee_bps: Decimal = Decimal("0"), native_to_quote_rate: Decimal = Decimal("0"), min_net_bps: Decimal = Decimal("15"), telemetry=None):
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
        self.telemetry = telemetry
        # Keep isolated RPC/Web3 adapters alive across scans. Rebuilding them per
        # candidate caused repeated chain/factory discovery and added avoidable latency.
        # Reuse the already-connected adapter by default. Fresh Web3 adapters perform
        # chain-id/factory RPC calls during initialization and can overwhelm public RPCs.
        # Isolation remains available for debugging with DEX_ISOLATE_QUOTE_ADAPTERS=true.
        self._quote_adapters: dict[str, object] = {}
        isolate = os.getenv("DEX_ISOLATE_QUOTE_ADAPTERS", "false").strip().lower() in {"1", "true", "yes", "on"}
        if isolate and hasattr(adapter, "clone_for_concurrent_quotes"):
            for source in self.sources:
                try:
                    self._quote_adapters[source] = adapter.clone_for_concurrent_quotes()
                except Exception as exc:
                    logging.warning("DEX adapter clone failed source=%s; falling back to shared adapter: %s", source, exc)
        if not self._quote_adapters:
            self._quote_adapters = {source: adapter for source in self.sources}
        self._isolated_quote_adapters = isolate and all(self._quote_adapters.get(source) is not adapter for source in self.sources)
        self.last_rejections: dict[str, int] = {}

    def _metric(self, name: str, amount: int = 1) -> None:
        if self.telemetry is not None:
            self.telemetry.increment(name, amount)

    def _gauge(self, name: str, value: int) -> None:
        if self.telemetry is not None:
            self.telemetry.set_gauge(name, value)

    def _reject(self, reason: str) -> None:
        self.last_rejections[reason] = self.last_rejections.get(reason, 0) + 1
        if "quote" in reason:
            self._metric("quote_failures")

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
        if gas_native is None or Decimal(str(gas_native)) <= 0:
            gas_native = Decimal(str(getattr(execution, "gas", 0))) * Decimal(str(getattr(execution, "gas_price", 0)))
        gas_native = Decimal(str(gas_native))
        if gas_native <= 0:
            return Decimal("0")
        if native_to_quote_rate <= 0:
            return Decimal("Infinity")
        return (gas_native / Decimal(10) ** 18) * native_to_quote_rate

    def _candidate_amounts(self, ceiling: int) -> list[int]:
        """Build a dynamic size grid from available capital/liquidity.

        The grid is deliberately independent of a fixed dollar size. It samples
        small, medium and near-ceiling notionals, then the optimizer can refine
        around the best observed region. This lets the same engine adapt from
        small balances to larger executable liquidity without pretending that
        a single size is optimal for every route.
        """
        if ceiling <= 0: return []
        configured = int(os.getenv("DEX_MAX_QUOTE_CANDIDATES", "4"))
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

    def _refinement_amounts(self, best_amount: int, ceiling: int, candidates: list[int]) -> list[int]:
        """Generate a local refinement grid around the best coarse size."""
        if best_amount <= 0 or ceiling <= 0:
            return []
        radius = Decimal(os.getenv("DEX_OPTIMIZER_RADIUS", "0.25"))
        steps = max(2, min(8, int(os.getenv("DEX_OPTIMIZER_STEPS", "4"))))
        radius = max(Decimal("0.05"), min(Decimal("0.75"), radius))
        lo = max(1, int(Decimal(best_amount) * (Decimal("1") - radius)))
        hi = min(ceiling, int(Decimal(best_amount) * (Decimal("1") + radius)))
        if hi <= lo:
            return []
        span = hi - lo
        refined = [lo + (span * i) // (steps + 1) for i in range(1, steps + 1)]
        refined.extend([best_amount, (lo + hi) // 2])
        return sorted(set(x for x in refined if 0 < x <= ceiling and x not in candidates))

    def fast_probe(self, *, chain_id: int, quote_token: str, base_token: str, quote_amount: int, taker: str, slippage_bps: int = 50) -> Decimal:
        """Fast gross-spread probe used to rank candidates before expensive sizing scans.

        It deliberately ignores gas and final execution economics. The full scan remains
        the only profitability gate. A failed or slow leg returns negative infinity.
        """
        if quote_amount <= 0:
            return Decimal("-Infinity")
        if len(self.sources) < 2:
            return Decimal("-Infinity")
        quotes: dict[str, tuple[DexQuote, DexExecution]] = {}
        for source in self.sources:
            try:
                started = time.perf_counter()
                quote, execution = self._quote_adapters.get(source, self.adapter).quote_single_source(
                    chain_id=chain_id, sell_token=quote_token, buy_token=base_token,
                    sell_amount=quote_amount, taker=taker, source=source, slippage_bps=slippage_bps,
                    deadline=time.perf_counter() + float(self.max_quote_latency_ms) / 1000.0,
                )
                latency = self._quote_latency(quote)
                if latency > self.max_quote_latency_ms:
                    self._reject("fast_probe_slow")
                    self._reject(f"fast_probe_slow_{source}")
                    continue
                if not self._quote_quality_ok(
                    quote, sell_token=quote_token, buy_token=base_token,
                    sell_amount=quote_amount, stage="fast_probe_buy"
                ):
                    continue
                quotes[source] = (quote, execution)
                logging.debug(
                    "fast probe buy source=%s base=%s amount=%s latency_ms=%.1f elapsed_ms=%.1f",
                    source, base_token, quote_amount, latency,
                    (time.perf_counter() - started) * 1000,
                )
            except Exception as exc:
                self._reject("fast_probe_error")
                self._reject(f"fast_probe_error_{source}")
                logging.debug("fast probe buy failed source=%s base=%s: %s", source, base_token, exc)
        if len(quotes) < 2:
            return Decimal("-Infinity")

        best = Decimal("-Infinity")
        for buy_source, (_, buy_execution) in quotes.items():
            for sell_source in self.sources:
                if sell_source == buy_source:
                    continue
                try:
                    sell_quote, sell_execution = self._quote_adapters.get(sell_source, self.adapter).quote_single_source(
                        chain_id=chain_id, sell_token=base_token, buy_token=quote_token,
                        sell_amount=buy_execution.buy_amount, taker=taker, source=sell_source,
                        slippage_bps=slippage_bps,
                        deadline=time.perf_counter() + float(self.max_quote_latency_ms) / 1000.0,
                    )
                    latency = self._quote_latency(sell_quote)
                    if latency > self.max_quote_latency_ms:
                        self._reject("fast_probe_slow")
                        self._reject(f"fast_probe_slow_{sell_source}")
                        continue
                    if not self._quote_quality_ok(
                        sell_quote, sell_token=base_token, buy_token=quote_token,
                        sell_amount=buy_execution.buy_amount, stage="fast_probe_sell"
                    ):
                        continue
                    if sell_execution.buy_amount <= 0:
                        continue
                    gross = (
                        Decimal(sell_execution.buy_amount) / Decimal(quote_amount)
                        - Decimal("1")
                    ) * Decimal("10000")
                    best = max(best, gross)
                except Exception as exc:
                    self._reject("fast_probe_error")
                    self._reject(f"fast_probe_error_{sell_source}")
                    logging.debug(
                        "fast probe sell failed buy_source=%s sell_source=%s base=%s: %s",
                        buy_source, sell_source, base_token, exc,
                    )
        return best

    def scan_max_profitable(self, *, chain_id: int, quote_token: str, base_token: str, max_quote_amount: Decimal, taker: str, slippage_bps: int = 50, compound_amount: int = 0) -> list[DexOpportunity]:
        if max_quote_amount <= 0: raise ValueError("max_quote_amount must be positive")
        scale = Decimal(10) ** self.quote_token_decimals; ceiling = int(max_quote_amount * scale)
        if ceiling <= 0: return []
        compound_amount = int(compound_amount)
        if compound_amount < 0 or compound_amount >= ceiling:
            raise ValueError("compound_amount must be non-negative and below the total quote ceiling")
        self.last_rejections = {}; profitable: list[DexOpportunity] = []
        # Size the flash-loan portion independently, then add retained profits to
        # every candidate. Aave liquidity remains the hard ceiling for the loan.
        loan_candidates = self._candidate_amounts(ceiling - compound_amount)
        candidates = [candidate + compound_amount for candidate in loan_candidates]

        # Candidate sizes are independent. Evaluate them concurrently so the scan
        # latency is bounded by the slowest candidate rather than the sum of all
        # candidate RPC round trips. Keep the worker count bounded for RPC safety.
        max_workers = max(1, min(len(candidates), int(os.getenv("DEX_CANDIDATE_CONCURRENCY", "1"))))
        def _scan(candidate: int):
            return self.scan_once(
                chain_id=chain_id, quote_token=quote_token, base_token=base_token,
                quote_amount=candidate, taker=taker, slippage_bps=slippage_bps,
                compound_amount=compound_amount,
            )
        if max_workers == 1:
            results = [_scan(candidate) for candidate in candidates]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_scan, candidates))
        for result in results:
            profitable.extend(result)

        # Dynamic second pass: refine around the best executable coarse size.
        # Every refined size goes through the same hard profitability gates.
        if profitable:
            best = max(profitable, key=lambda x: (x.net_profit_quote, x.quote_amount))
            refined = [
                amount for amount in self._refinement_amounts(best.quote_amount, ceiling, candidates)
                if amount > compound_amount
            ]
            if refined:
                logging.info(
                    "DEX dynamic optimizer coarse_best=%s refined_candidates=%s ceiling=%s",
                    best.quote_amount, refined, ceiling,
                )
                if max_workers == 1:
                    refined_results = [_scan(candidate) for candidate in refined]
                else:
                    with ThreadPoolExecutor(max_workers=max_workers) as pool:
                        refined_results = list(pool.map(_scan, refined))
                for result in refined_results:
                    profitable.extend(result)

        if not profitable:
            logging.info(
                "DEX dynamic optimizer found no executable positive route base=%s candidates=%s",
                base_token, candidates,
            )
            return []
        best = max(profitable, key=lambda x: (x.net_profit_quote, x.net_profit_quote / Decimal(x.quote_amount)))
        self._metric("optimal_size_found")
        logging.info(
            "DEX dynamic optimizer selected buy=%s sell=%s base=%s amount_raw=%s net=%s",
            best.buy_source, best.sell_source, base_token, best.quote_amount, best.net_profit_quote,
        )
        return [best]

    def scan_once(self, *, chain_id: int, quote_token: str, base_token: str, quote_amount: int, taker: str, slippage_bps: int = 50, compound_amount: int = 0) -> list[DexOpportunity]:
        if len(self.sources) < 2: raise ValueError("DEX cross-exchange mode requires at least two DEX sources")
        if quote_amount <= 0: raise ValueError("quote_amount must be positive")
        if compound_amount < 0 or compound_amount >= quote_amount:
            raise ValueError("compound_amount must be non-negative and below quote_amount")
        if not 0 <= slippage_bps <= 5000: raise ValueError("slippage_bps must be between 0 and 5000")
        flash_amount = quote_amount - compound_amount
        quote_adapters = self._quote_adapters
        buy_quotes: dict[str, tuple[DexQuote, DexExecution]] = {}
        sell_sources: set[str] = set()
        opportunities: list[DexOpportunity] = []

        def _buy(source):
            return source, quote_adapters.get(source, self.adapter).quote_single_source(
                chain_id=chain_id, sell_token=quote_token, buy_token=base_token,
                sell_amount=quote_amount, taker=taker, source=source, slippage_bps=slippage_bps,
                deadline=time.perf_counter() + float(self.max_quote_latency_ms) / 1000.0,
            )

        # All venue quotes are independent. Run them concurrently with a bounded worker pool
        # so adding venues improves coverage without creating unbounded threads.
        max_quote_workers = max(1, min(len(self.sources), int(os.getenv("DEX_QUOTE_CONCURRENCY", "1"))))
        pool = ThreadPoolExecutor(max_workers=max_quote_workers)
        futures = [pool.submit(_buy, source) for source in self.sources]
        done, pending = wait(futures, timeout=float(self.max_quote_latency_ms) / 1000)
        for future in done:
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
                self._metric("quote_observations")

        if pending:
            self._reject("buy_quote_timeout")
            logging.info("DEX buy quote deadline expired pending=%s limit_ms=%s", len(pending), self.max_quote_latency_ms)
        pool.shutdown(wait=False, cancel_futures=True)
        # This engine receives fresh executable quotes rather than raw pool logs.
        # Keep the requested pool-state gauge explicit instead of pretending that
        # quote polling is equivalent to a pool-event subscription.
        self._gauge("pools_with_fresh_state", len(buy_quotes))

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
                deadline=time.perf_counter() + float(self.max_quote_latency_ms) / 1000.0,
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
            self._metric("quote_observations")
            sell_sources.add(source)

            scale = Decimal(10) ** self.quote_token_decimals
            flash_loan_fee_quote = (
                Decimal(flash_amount) / scale * self.flash_loan_fee_bps / Decimal("10000")
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
            self._metric("opportunities_after_slippage")

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
            if gross > 0:
                self._metric("opportunities_detected")
            if gross - flash_loan_fee_quote > 0:
                self._metric("opportunities_after_fees")
            if gross - gas_cost_quote - flash_loan_fee_quote > 0:
                self._metric("opportunities_after_gas")
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
            # Explicitly classify negative crosses for telemetry. These are
            # observations, not trade signals, and never bypass profit gates.
            if gross <= 0:
                self._reject("cross_gross_negative")
            elif net <= 0:
                self._reject("cross_net_negative_after_costs")
            if net > 0:
                self._metric("opportunities_after_mev_buffer")
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
                compound_amount=compound_amount,
            ))

        # Sell legs are independent. Run them concurrently even when the adapter
        # is shared. The adapter serializes individual RPC calls with its bounded
        # semaphore, while concurrency removes the unnecessary venue-by-venue wait.
        if sell_jobs:
            max_sell_workers = max(1, min(len(sell_jobs), int(os.getenv("DEX_SELL_CONCURRENCY", "1"))))
            pool = ThreadPoolExecutor(max_workers=max_sell_workers)
            futures = {pool.submit(_sell, job): job for job in sell_jobs}
            done, pending = wait(futures, timeout=float(self.max_quote_latency_ms) / 1000.0)
            for future in done:
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
            if pending:
                self._reject("sell_quote_timeout")
                logging.info("DEX sell quote deadline expired pending=%s limit_ms=%s", len(pending), self.max_quote_latency_ms)
            pool.shutdown(wait=False, cancel_futures=True)

        if not buy_quotes: self._reject("no_buy_sources")
        if not sell_sources and buy_quotes: self._reject("no_sell_sources")
        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
