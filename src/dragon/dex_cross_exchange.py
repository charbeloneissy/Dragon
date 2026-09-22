from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass, replace
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
    flash_multiplier: Decimal = Decimal("0")

    @property
    def flash_loan_amount(self) -> int:
        return self.quote_amount - self.compound_amount


class DexCrossExchangeEngine:
    """Two-leg cross-DEX scanner with conservative net-profit accounting."""

    def __init__(self, adapter, sources: Iterable[str], min_profit: Decimal = Decimal("0.002"), quote_token_decimals: int = 6, max_quote_latency_ms: Decimal = Decimal(os.getenv("DEX_MAX_QUOTE_LATENCY_MS", "500")), safety_buffer_quote: Decimal = Decimal("0"), flash_loan_enabled: bool = False, flash_loan_fee_bps: Decimal = Decimal("0"), native_to_quote_rate: Decimal = Decimal("0"), min_net_bps: Decimal = Decimal("15"), telemetry=None):
        if quote_token_decimals < 0 or quote_token_decimals > 36: raise ValueError("quote_token_decimals must be between 0 and 36")
        if Decimal(min_profit) < Decimal("0.002"): raise ValueError("min_profit cannot be below 0.002")
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
        self.max_quote_latency_ms = min(Decimal(max_quote_latency_ms), Decimal(os.getenv("DEX_HARD_QUOTE_DEADLINE_MS", "500")))
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
        # Adaptive quote deadline: start at the hard ceiling, then tighten/expand
        # from observed live quote latency without ever exceeding that ceiling.
        self._latency_ewma_ms: float | None = None
        self._latency_samples = 0

    def _observe_quote_latency(self, quote: DexQuote) -> None:
        latency = self._quote_latency(quote)
        if not latency.is_finite() or latency < 0:
            return
        value = float(latency)
        alpha = min(0.5, max(0.05, float(os.getenv("DEX_LATENCY_EWMA_ALPHA", "0.25"))))
        previous = self._latency_ewma_ms
        self._latency_ewma_ms = value if previous is None else (alpha * value + (1.0 - alpha) * previous)
        self._latency_samples += 1

    def _dynamic_quote_deadline_ms(self) -> Decimal:
        """Choose a live quote deadline from observed latency."""
        hard_max = min(Decimal(self.max_quote_latency_ms), Decimal(os.getenv("DEX_HARD_QUOTE_DEADLINE_MS", "500")))
        floor = Decimal(os.getenv("DEX_MIN_DYNAMIC_QUOTE_DEADLINE_MS", "100"))
        multiplier = Decimal(os.getenv("DEX_DYNAMIC_DEADLINE_MULTIPLIER", "2.0"))
        headroom = Decimal(os.getenv("DEX_DYNAMIC_DEADLINE_HEADROOM_MS", "50"))
        if floor <= 0 or multiplier <= 0 or headroom < 0:
            raise ValueError("dynamic quote deadline parameters are invalid")
        floor = min(floor, hard_max)
        if self._latency_ewma_ms is None or self._latency_samples < 3:
            return hard_max
        target = Decimal(str(self._latency_ewma_ms)) * multiplier + headroom
        return min(hard_max, max(floor, target))

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

    def _candidate_amounts(self, ceiling: int, floor: int = 1) -> list[int]:
        """Build an adaptive economic sizing grid for the opportunity brain.

        It mixes low-size discovery, geometric liquidity expansion and ceiling
        probes. The second pass then zooms into the best observed net-economic
        regions. No fixed dollar size is assumed to be optimal.
        """
        floor = max(1, int(floor))
        if ceiling < floor or ceiling <= 0:
            return []
        configured = int(os.getenv("DEX_MAX_QUOTE_CANDIDATES", "12"))
        max_candidates = max(6, min(40, configured))
        if ceiling == floor:
            return [floor]

        span = ceiling - floor
        # Tiny profitable pockets are easy to miss with a linear grid because
        # price impact can change sharply at concentrated-liquidity boundaries.
        fractions = [
            Decimal("0"), Decimal("0.01"), Decimal("0.02"), Decimal("0.05"),
            Decimal("0.10"), Decimal("0.20"), Decimal("0.35"), Decimal("0.50"),
            Decimal("0.65"), Decimal("0.80"), Decimal("0.90"), Decimal("0.97"),
            Decimal("1.00"),
        ]
        if max_candidates < len(fractions):
            keep = [fractions[0], fractions[1], fractions[3], fractions[-2], fractions[-1]]
            middle = fractions[4:-2]
            remaining = max_candidates - len(keep)
            for i in range(max(0, remaining)):
                idx = round(i * (len(middle) - 1) / max(1, remaining - 1))
                keep.append(middle[idx])
            fractions = sorted(set(keep))
        elif max_candidates > len(fractions):
            for i in range(1, max_candidates - len(fractions) + 1):
                fractions.append(Decimal(i) / Decimal(max_candidates + 1))

        amounts = {floor + int(Decimal(span) * f) for f in fractions}
        amounts.update({floor, ceiling})
        return sorted(x for x in amounts if floor <= x <= ceiling and x > 0)

    def _adaptive_refinement_amounts(self, surface: list[DexOpportunity], ceiling: int, floor: int, candidates: list[int]) -> list[int]:
        """Find extra sizes where the observed profit curve changes shape.

        The calculator follows up to three economic peaks per route and probes
        between improving points. This catches narrow arbitrage pockets caused
        by price impact or concentrated liquidity without an unbounded RPC scan.
        """
        if len(surface) < 2 or ceiling <= floor:
            return []
        by_route: dict[tuple[str, str, str], list[DexOpportunity]] = {}
        for op in surface:
            key = (op.buy_source, op.sell_source, op.base_token)
            by_route.setdefault(key, []).append(op)

        proposals: set[int] = set()
        radius = Decimal(os.getenv("DEX_OPTIMIZER_RADIUS", "0.20"))
        steps = max(2, min(10, int(os.getenv("DEX_OPTIMIZER_STEPS", "6"))))
        radius = max(Decimal("0.05"), min(Decimal("0.60"), radius))

        for points in by_route.values():
            points.sort(key=lambda x: x.quote_amount)
            peaks = sorted(
                points,
                key=lambda x: (
                    x.net_profit_quote,
                    x.net_profit_quote / Decimal(max(1, x.quote_amount)),
                ),
                reverse=True,
            )[:3]
            for peak in peaks:
                lo = max(floor, int(Decimal(peak.quote_amount) * (Decimal("1") - radius)))
                hi = min(ceiling, int(Decimal(peak.quote_amount) * (Decimal("1") + radius)))
                if hi <= lo:
                    continue
                span = hi - lo
                for i in range(1, steps + 1):
                    proposals.add(lo + (span * i) // (steps + 1))

            # If net profit is improving between two observations, inspect the
            # midpoint because the curve may bend before the next coarse point.
            for left, right in zip(points, points[1:]):
                if right.quote_amount > left.quote_amount and right.net_profit_quote > left.net_profit_quote:
                    proposals.add((left.quote_amount + right.quote_amount) // 2)

        existing = set(candidates)
        return sorted(x for x in proposals if floor <= x <= ceiling and x not in existing)


    def _refinement_amounts(self, best_amount: int, ceiling: int, candidates: list[int], floor: int = 1) -> list[int]:
        """Generate a local refinement grid around the best coarse size."""
        if best_amount <= 0 or ceiling <= 0:
            return []
        radius = Decimal(os.getenv("DEX_OPTIMIZER_RADIUS", "0.25"))
        steps = max(2, min(8, int(os.getenv("DEX_OPTIMIZER_STEPS", "4"))))
        radius = max(Decimal("0.05"), min(Decimal("0.75"), radius))
        floor = max(1, int(floor))
        lo = max(floor, int(Decimal(best_amount) * (Decimal("1") - radius)))
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
                    probe=True,
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
                self._reject(f"fast_probe_error_{source}_{type(exc).__name__}")
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
                        probe=True,
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
                    self._reject(f"fast_probe_error_{sell_source}_{type(exc).__name__}")
                    logging.debug(
                        "fast probe sell failed buy_source=%s sell_source=%s base=%s: %s",
                        buy_source, sell_source, base_token, exc,
                    )
        return best

    def scan_max_profitable(self, *, chain_id: int, quote_token: str, base_token: str, max_quote_amount: Decimal, taker: str, slippage_bps: int = 50, compound_amount: int = 0) -> list[DexOpportunity]:
        if max_quote_amount <= 0: raise ValueError("max_quote_amount must be positive")
        scale = Decimal(10) ** self.quote_token_decimals
        configured_min = Decimal(os.getenv("DEX_MIN_FLASH_LOAN_QUOTE", "100"))
        configured_max = Decimal(os.getenv("DEX_MAX_FLASH_LOAN_QUOTE", "3000"))
        if not configured_min.is_finite() or configured_min <= 0:
            raise ValueError("DEX_MIN_FLASH_LOAN_QUOTE must be positive")
        if not configured_max.is_finite() or configured_max < configured_min:
            raise ValueError("DEX_MAX_FLASH_LOAN_QUOTE must be >= DEX_MIN_FLASH_LOAN_QUOTE")
        effective_max = min(Decimal(max_quote_amount), configured_max)
        ceiling = int(effective_max * scale)
        floor = int(configured_min * scale)
        if ceiling < floor or ceiling <= 0: return []
        compound_amount = int(compound_amount)
        if compound_amount != 0:
            raise ValueError("Dragon is configured for no compounding; compound_amount must be zero")
        self.last_rejections = {}; profitable: list[DexOpportunity] = []
        # Size the flash-loan portion independently, then add retained profits to
        # every candidate. Aave liquidity remains the hard ceiling for the loan.
        loan_candidates = self._candidate_amounts(ceiling - compound_amount, floor=floor)
        candidates = [candidate + compound_amount for candidate in loan_candidates]

        # Candidate sizes are independent. Evaluate them concurrently so the scan
        # latency is bounded by the slowest candidate rather than the sum of all
        # candidate RPC round trips. Keep the worker count bounded for RPC safety.
        max_workers = max(1, min(len(candidates), int(os.getenv("DEX_CANDIDATE_CONCURRENCY", "1"))))
        def _scan(candidate: int, enforce_profit_gate: bool = False):
            return self.scan_once(
                chain_id=chain_id, quote_token=quote_token, base_token=base_token,
                quote_amount=candidate, taker=taker, slippage_bps=slippage_bps,
                compound_amount=compound_amount, enforce_profit_gate=enforce_profit_gate,
            )
        # First build a profit surface without applying the final minimum-profit gate.
        # This is important: a coarse point can be negative while the true optimum
        # between two points is positive. The optimizer must learn the shape of the
        # curve before deciding whether the route is executable.
        if max_workers == 1:
            coarse_results = [_scan(candidate, False) for candidate in candidates]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                coarse_results = list(pool.map(lambda candidate: _scan(candidate, False), candidates))
        surface = [op for result in coarse_results for op in result]
        if not surface:
            logging.info(
                "DEX dynamic optimizer no executable quotes for profit surface base=%s candidates=%s",
                base_token, candidates,
            )
            return []

        # Refine around the best observed NET point even when that point is still
        # negative. This avoids missing narrow positive regions between coarse sizes.
        best_surface = max(surface, key=lambda x: (x.net_profit_quote, x.quote_amount))
        adaptive_refined = self._adaptive_refinement_amounts(
            surface, ceiling, floor, candidates
        )
        local_refined = self._refinement_amounts(
            best_surface.quote_amount, ceiling, candidates, floor=floor
        )
        refined = [
            amount for amount in sorted(set(adaptive_refined + local_refined))
            if amount > compound_amount
        ]
        if refined:
            logging.info(
                "DEX dynamic profit curve coarse_best=%s coarse_net=%s refined_candidates=%s ceiling=%s",
                best_surface.quote_amount, best_surface.net_profit_quote, refined, ceiling,
            )
            if max_workers == 1:
                refined_results = [_scan(candidate, False) for candidate in refined]
            else:
                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    refined_results = list(pool.map(lambda candidate: _scan(candidate, False), refined))
            surface.extend(op for result in refined_results for op in result)

        # Final hard gate is applied only after curve exploration and refinement.
        profitable = [
            op for op in surface
            if op.net_profit_quote >= self.min_profit
            and ((op.net_profit_quote / (Decimal(op.quote_amount) / scale)) * Decimal("10000")) >= self.min_net_bps
        ]
        if not profitable:
            best_surface = max(surface, key=lambda x: (x.net_profit_quote, x.quote_amount))
            logging.info(
                "DEX dynamic optimizer no positive executable point base=%s best_amount=%s best_net=%s min_profit=%s candidates=%s",
                base_token, best_surface.quote_amount, best_surface.net_profit_quote, self.min_profit, candidates + refined,
            )
            return []
        best = max(profitable, key=lambda x: (x.net_profit_quote, x.net_profit_quote / Decimal(x.quote_amount)))
        # Compute the multiplier only after route selection and executable-size
        # optimization. 1x equals the minimum 5% sizing probe.
        minimum_probe = configured_min
        selected_human = Decimal(best.quote_amount) / (Decimal(10) ** self.quote_token_decimals)
        multiplier = selected_human / minimum_probe
        best = replace(best, flash_multiplier=multiplier)
        self._metric("optimal_size_found")
        logging.info(
            "DEX dynamic optimizer selected buy=%s sell=%s base=%s amount_raw=%s net=%s",
            best.buy_source, best.sell_source, base_token, best.quote_amount, best.net_profit_quote,
        )
        return [best]

    def scan_once(self, *, chain_id: int, quote_token: str, base_token: str, quote_amount: int, taker: str, slippage_bps: int = 50, compound_amount: int = 0, enforce_profit_gate: bool = True) -> list[DexOpportunity]:
        if len(self.sources) < 2: raise ValueError("DEX cross-exchange mode requires at least two DEX sources")
        if quote_amount <= 0: raise ValueError("quote_amount must be positive")
        if compound_amount != 0:
            raise ValueError("Dragon is configured for no compounding; compound_amount must be zero")
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
        quote_deadline_ms = self._dynamic_quote_deadline_ms()
        logging.debug("DEX adaptive quote deadline_ms=%s ewma_ms=%s samples=%s", quote_deadline_ms, self._latency_ewma_ms, self._latency_samples)
        def _buy_dynamic(source):
            return source, quote_adapters.get(source, self.adapter).quote_single_source(
                chain_id=chain_id, sell_token=quote_token, buy_token=base_token,
                sell_amount=quote_amount, taker=taker, source=source, slippage_bps=slippage_bps,
                deadline=time.perf_counter() + float(quote_deadline_ms) / 1000.0,
            )
        max_quote_workers = max(1, min(len(self.sources), int(os.getenv("DEX_QUOTE_CONCURRENCY", "1"))))
        pool = ThreadPoolExecutor(max_workers=max_quote_workers)
        futures = {pool.submit(_buy_dynamic, source): source for source in self.sources}
        done, pending = wait(futures, timeout=float(quote_deadline_ms) / 1000)
        for future in done:
                source = futures[future]
                try:
                    source, (quote, execution) = future.result()
                except Exception as exc:
                    category = "INFRA_FAILURE" if any(x in str(exc).lower() for x in ("deadline", "rate limit", "rate-limit", "endpoint", "timeout", "rpc")) else "QUOTE_FAILURE"
                    logging.log(logging.INFO if "cached capability miss" in str(exc).lower() else logging.WARNING, "DEX buy quote failed category=%s source=%s sell=%s buy=%s amount=%s error=%s: %s", category, source, quote_token, base_token, quote_amount, type(exc).__name__, exc)
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
                self._observe_quote_latency(quote)
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

        sell_deadline_ms = self._dynamic_quote_deadline_ms()
        def _sell(job):
            buy_source, buy_quote, buy_execution, source, bought_amount = job
            sell_adapter = quote_adapters.get(source, self.adapter)
            return buy_source, buy_quote, buy_execution, source, sell_adapter.quote_single_source(
                chain_id=chain_id, sell_token=base_token, buy_token=quote_token,
                sell_amount=bought_amount, taker=taker, source=source, slippage_bps=slippage_bps,
                deadline=time.perf_counter() + float(sell_deadline_ms) / 1000.0,
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
            if enforce_profit_gate and net < self.min_profit:
                self._reject("net_profit_below_min")
                return
            if enforce_profit_gate and net_bps < self.min_net_bps:
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
                    sell_result = future.result()
                    self._observe_quote_latency(sell_result[4][0])
                    _process_sell(job, sell_result)
                except Exception as exc:
                    source = job[3]
                    logging.warning(
                        "DEX sell quote failed source=%s sell=%s buy=%s amount=%s error=%s: %s",
                        source, base_token, quote_token, job[4], type(exc).__name__, exc,
                    )
                    self._reject("sell_quote_error")
            if pending:
                self._reject("sell_quote_timeout")
                logging.info("DEX sell quote deadline expired pending=%s limit_ms=%s", len(pending), sell_deadline_ms)
            pool.shutdown(wait=False, cancel_futures=True)

        if not buy_quotes: self._reject("no_buy_sources")
        if not sell_sources and buy_quotes: self._reject("no_sell_sources")
        return sorted(opportunities, key=lambda x: x.net_profit_quote, reverse=True)
