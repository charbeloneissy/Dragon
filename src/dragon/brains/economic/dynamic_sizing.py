"""Fast, state-aware trade-size optimization for Dragon.

This module is deliberately execution-agnostic: it prices candidate sizes,
models execution probability, searches the profitable region adaptively, and
returns a deterministic sizing decision. It never signs, broadcasts, or moves
funds.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Callable, Iterable, Sequence

D = Decimal
ZERO = D("0")
ONE = D("1")


def dec(value: object, default: D = ZERO) -> D:
    try:
        x = D(str(value))
        return x if x.is_finite() else default
    except (InvalidOperation, TypeError, ValueError):
        return default


def clamp01(value: D) -> D:
    return max(ZERO, min(ONE, value))


@dataclass(frozen=True)
class EconomicInputs:
    """Route-level costs which are independent or semi-independent of size."""
    flash_fee_rate: D = ZERO
    gas_quote: D = ZERO
    minimum_net: D = D("0.005")


@dataclass(frozen=True)
class SizeQuote:
    """A live quote for one exact trade size."""
    size: D
    gross_spread: D
    dex_fees: D
    flash_fee: D
    gas: D
    slippage: D
    execution_probability: D = ONE
    quote_version: str = ""

    @property
    def net(self) -> D:
        return self.gross_spread - self.dex_fees - self.flash_fee - self.gas - self.slippage

    @property
    def expected_net(self) -> D:
        return self.net * clamp01(self.execution_probability)


@dataclass(frozen=True)
class SizeEvaluation:
    quote: SizeQuote
    marginal_net: D

    @property
    size(self) -> D:
        return self.quote.size

    @property
    def net(self) -> D:
        return self.quote.net

    @property
    def expected_net(self) -> D:
        return self.quote.expected_net


@dataclass(frozen=True)
class ImpactFingerprint:
    """Canonical state identity used to decide whether cached sizing is reusable."""
    chain_id: int
    venue_a: str
    venue_b: str
    token_in: str
    token_mid: str
    liquidity_a: D
    liquidity_b: D
    price_a: D
    price_b: D
    fee_a: D
    fee_b: D
    volatility: D
    state_version: str = ""

    def key(self) -> str:
        raw = "|".join([
            str(self.chain_id), self.venue_a.lower(), self.venue_b.lower(),
            self.token_in.lower(), self.token_mid.lower(),
            str(self.liquidity_a), str(self.liquidity_b),
            str(self.price_a), str(self.price_b),
            str(self.fee_a), str(self.fee_b), str(self.volatility),
            self.state_version,
        ])
        return sha256(raw.encode()).hexdigest()


QuoteFn = Callable[[D], SizeQuote]


class AdaptiveSizingEngine:
    """Coarse-to-fine optimizer with state-aware cache and local refinement.

    The optimizer uses a small initial probe set, identifies the strongest
    region, then searches locally. The caller owns the live quote function.
    """

    def __init__(
        self,
        *,
        minimum_size: D = D("0.00000001"),
        max_quote_calls: int = 9,
        refinement_rounds: int = 2,
        minimum_net: D = D("0.005"),
        cache_state_tolerance: D = D("0"),
    ) -> None:
        if max_quote_calls < 3:
            raise ValueError("max_quote_calls must be >= 3")
        if refinement_rounds < 0:
            raise ValueError("refinement_rounds must be >= 0")
        self.minimum_size = dec(minimum_size)
        self.max_quote_calls = max_quote_calls
        self.refinement_rounds = refinement_rounds
        self.minimum_net = dec(minimum_net)
        self.cache_state_tolerance = max(ZERO, dec(cache_state_tolerance))
        self._cache: dict[str, tuple[D, D, str]] = {}

    @staticmethod
    def _best(evaluations: Iterable[SizeEvaluation]) -> SizeEvaluation | None:
        rows = list(evaluations)
        return max(rows, key=lambda x: (x.expected_net, x.net, x.size), default=None)

    @staticmethod
    def _marginal(prev: SizeQuote | None, current: SizeQuote) -> D:
        if prev is None or current.size == prev.size:
            return ZERO
        return (current.net - prev.net) / (current.size - prev.size)

    def _evaluate(self, quote_fn: QuoteFn, size: D, previous: SizeQuote | None, seen: dict[D, SizeEvaluation]) -> SizeEvaluation:
        size = max(self.minimum_size, dec(size))
        if size in seen:
            return seen[size]
        quote = quote_fn(size)
        if quote.size != size:
            quote = SizeQuote(
                size=size, gross_spread=quote.gross_spread, dex_fees=quote.dex_fees,
                flash_fee=quote.flash_fee, gas=quote.gas, slippage=quote.slippage,
                execution_probability=quote.execution_probability, quote_version=quote.quote_version,
            )
        ev = SizeEvaluation(quote=quote, marginal_net=self._marginal(previous, quote))
        seen[size] = ev
        return ev

    def optimize(self, *, min_size: D, max_size: D, quote_fn: QuoteFn, fingerprint: ImpactFingerprint | None = None) -> dict:
        lo, hi = dec(min_size), dec(max_size)
        if lo <= ZERO or hi < lo:
            raise ValueError("invalid size range")
        if lo < self.minimum_size:
            lo = self.minimum_size
        if hi < lo:
            raise ValueError("max_size is below minimum executable size")

        cache_key = fingerprint.key() if fingerprint else None
        if cache_key and cache_key in self._cache:
            cached_size, cached_expected, cached_version = self._cache[cache_key]
            if lo <= cached_size <= hi:
                return {
                    "status": "cached",
                    "size": cached_size,
                    "expected_net": cached_expected,
                    "quote_version": cached_version,
                    "quote_calls": 0,
                    "fingerprint": cache_key,
                }

        seen: dict[D, SizeEvaluation] = {}
        calls = 0

        def probe(size: D, prev: SizeQuote | None = None) -> SizeEvaluation:
            nonlocal calls
            if size not in seen:
                if calls >= self.max_quote_calls:
                    raise RuntimeError("quote_budget_exhausted")
                calls += 1
            return self._evaluate(quote_fn, size, prev, seen)

        if lo == hi:
            best = probe(lo)
        else:
            # Geometric probes cover a wide size range without a linear scan.
            span = hi / lo
            if span > D("16"):
                fractions = (D("0"), D("0.125"), D("0.35"), D("0.65"), D("1"))
            else:
                fractions = (D("0"), D("0.25"), D("0.5"), D("0.75"), D("1"))
            for f in fractions:
                probe(lo + (hi - lo) * f)
            best = self._best(seen.values())
            assert best is not None

            for _ in range(self.refinement_rounds):
                if calls + 2 > self.max_quote_calls:
                    break
                ordered = sorted(seen.values(), key=lambda x: x.expected_net, reverse=True)
                center = ordered[0].size
                left = max(lo, (center + lo) / D("2"))
                right = min(hi, (center + hi) / D("2"))
                for size in (left, right):
                    if size != center and size not in seen and calls < self.max_quote_calls:
                        probe(size)
                best = self._best(seen.values())
                assert best is not None

        assert best is not None
        if cache_key:
            self._cache[cache_key] = (best.size, best.expected_net, best.quote.quote_version)

        return {
            "status": "optimized",
            "size": best.size,
            "net": best.net,
            "expected_net": best.expected_net,
            "marginal_net": best.marginal_net,
            "profitable": best.expected_net >= self.minimum_net,
            "quote_calls": calls,
            "fingerprint": cache_key,
            "evaluations": tuple(
                {"size": x.size, "net": x.net, "expected_net": x.expected_net, "marginal_net": x.marginal_net}
                for x in sorted(seen.values(), key=lambda x: x.size)
            ),
        }

    def invalidate(self, fingerprint: ImpactFingerprint) -> None:
        self._cache.pop(fingerprint.key(), None)

    def clear_cache(self) -> None:
        self._cache.clear()
