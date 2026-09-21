"""Three-leg triangular arbitrage scanner.

Each leg is quoted independently. The output of leg N is the exact input to
leg N+1; no theoretical intermediate amount is substituted. This module is a
quote/calculation layer only and never signs or broadcasts a transaction.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from itertools import permutations, product
from typing import Callable, Iterable

from .leg_brain import LegBrain, LegObservation


@dataclass(frozen=True)
class TriangularRoute:
    chain_id: int
    venues: tuple[str, str, str]
    token_a: str
    token_b: str
    token_c: str


@dataclass(frozen=True)
class TriangularOpportunity:
    route: TriangularRoute
    quote_amount: int
    leg_amounts: tuple[int, int, int]
    final_amount: int
    gross_profit_quote: Decimal
    gas_cost_quote: Decimal
    flash_loan_fee_quote: Decimal
    net_profit_quote: Decimal
    legs: tuple[object, object, object]
    reason: str = "validated"


class TriangularEngine:
    """Dynamic three-leg optimizer with independent leg brains."""

    def __init__(
        self,
        adapter,
        *,
        min_profit: Decimal = Decimal("0.005"),
        quote_decimals: int = 6,
        flash_fee_bps: Decimal = Decimal("0"),
        native_to_quote_rate: Decimal = Decimal("0"),
        max_quote_age_ms: Decimal = Decimal("500"),
        max_workers: int = 24,
    ):
        if Decimal(min_profit) < Decimal("0.005"):
            raise ValueError("min_profit cannot be below 0.005")
        self.adapter = adapter
        self.min_profit = Decimal(min_profit)
        self.quote_decimals = int(quote_decimals)
        self.flash_fee_bps = Decimal(flash_fee_bps)
        self.native_to_quote_rate = Decimal(native_to_quote_rate)
        self.max_quote_age_ms = Decimal(max_quote_age_ms)
        self.max_workers = max(3, int(max_workers))
        self._brains: dict[tuple[int, str, str, str], LegBrain] = {}
        self.last_best: dict[TriangularRoute, TriangularOpportunity] = {}

    def _brain(self, route: TriangularRoute, leg: int) -> LegBrain:
        key = (route.chain_id, route.venues[leg], (route.token_a, route.token_b, route.token_c)[leg],
               (route.token_b, route.token_c, route.token_a)[leg])
        return self._brains.setdefault(key, LegBrain(max_quote_age_ms=self._max_age))

    @property
    def _max_age(self) -> Decimal:
        return self.max_quote_age_ms

    @staticmethod
    def candidate_amounts(max_amount: int, *, points: int = 9) -> tuple[int, ...]:
        if max_amount <= 0:
            return ()
        fractions = (Decimal("0.02"), Decimal("0.05"), Decimal("0.10"), Decimal("0.20"),
                     Decimal("0.35"), Decimal("0.50"), Decimal("0.70"), Decimal("0.85"), Decimal("1"))
        return tuple(sorted({max(1, int(Decimal(max_amount) * f)) for f in fractions[:max(2, min(points, len(fractions)))]}))

    def _quote(self, route: TriangularRoute, amount: int, venue: str, sell: str, buy: str):
        return self.adapter.quote_single_source(
            chain_id=route.chain_id, sell_token=sell, buy_token=buy,
            sell_amount=amount, source=venue, probe=False,
        )

    def evaluate(self, route: TriangularRoute, amount: int) -> TriangularOpportunity | None:
        if len({route.token_a.lower(), route.token_b.lower(), route.token_c.lower()}) != 3:
            return None
        if len(set(route.venues)) < 1 or amount <= 0:
            return None
        legs = []
        current = amount
        pairs = ((route.token_a, route.token_b), (route.token_b, route.token_c), (route.token_c, route.token_a))
        try:
            for idx, (sell, buy) in enumerate(pairs):
                quote, execution = self._quote(route, current, route.venues[idx], sell, buy)
                buy_amount = int(getattr(quote, "buy_amount", 0))
                if buy_amount <= 0:
                    return None
                brain = self._brain(route, idx)
                decision = brain.decide(LegObservation(
                    chain_id=route.chain_id, source=route.venues[idx], sell_token=sell,
                    buy_token=buy, sell_amount=current, buy_amount=buy_amount,
                    latency_ms=Decimal(str(getattr(quote, "latency_ms", 0))),
                    gas_cost_quote=Decimal(str(getattr(quote, "gas_quote", 0))),
                    quote_age_ms=Decimal("0"),
                    liquidity_ok=True,
                ))
                if not decision.executable:
                    return None
                legs.append(execution or quote)
                current = buy_amount
            scale = Decimal(10) ** self.quote_decimals
            gross = Decimal(current - amount) / scale
            gas = sum((Decimal(str(getattr(q, "gas_quote", 0))) for q in legs), Decimal("0"))
            flash_fee = (Decimal(amount) / scale) * self.flash_fee_bps / Decimal("10000")
            net = gross - gas - flash_fee
            return TriangularOpportunity(route, amount, (amount, *[getattr(x, "buy_amount", 0) for x in legs[:2]]),
                                         current, gross, gas, flash_fee, net, tuple(legs))
        except Exception as exc:
            logging.debug("triangular route failed chain=%s venues=%s: %s", route.chain_id, route.venues, exc)
            return None

    def optimize(self, routes: Iterable[TriangularRoute], max_amount: int) -> list[TriangularOpportunity]:
        """Explore the profit surface, refine around the best point, then gate.

        The coarse grid is deliberately allowed to be negative. A negative
        coarse point must never hide a profitable local peak caused by AMM
        price impact. Every refinement re-quotes all three legs using the exact
        output of the previous leg.
        """
        route_list = tuple(routes)
        coarse = self.candidate_amounts(max_amount)
        if not route_list or not coarse:
            return []

        def evaluate_route(route: TriangularRoute) -> TriangularOpportunity | None:
            best: TriangularOpportunity | None = None
            for amount in coarse:
                candidate = self.evaluate(route, amount)
                if candidate is not None and (best is None or candidate.net_profit_quote > best.net_profit_quote):
                    best = candidate
            if best is None:
                return None

            # Local refinement around the best observed size. Keep the search
            # bounded so aggressive discovery cannot turn into unbounded RPC
            # fan-out.
            ordered = tuple(sorted(coarse))
            idx = min(range(len(ordered)), key=lambda i: abs(ordered[i] - best.quote_amount))
            lo = ordered[max(0, idx - 1)]
            hi = ordered[min(len(ordered) - 1, idx + 1)]
            if hi > lo:
                span = hi - lo
                refined = {
                    max(1, lo + (span * n) // 8)
                    for n in range(1, 8)
                }
                for amount in sorted(refined):
                    candidate = self.evaluate(route, amount)
                    if candidate is not None and candidate.net_profit_quote > best.net_profit_quote:
                        best = candidate

            return best

        candidates: list[TriangularOpportunity] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for best in pool.map(evaluate_route, route_list):
                # Hard gate is applied only after the complete profit-surface
                # search/refinement for this route.
                if best is not None:
                    self.last_best[best.route] = best
                    if best.net_profit_quote >= self.min_profit:
                        candidates.append(best)

        return sorted(candidates, key=lambda x: x.net_profit_quote, reverse=True)

    @staticmethod
    def discover_routes(chain_id: int, venues: Iterable[str], token_cycle: tuple[str, str, str]) -> tuple[TriangularRoute, ...]:
        names = tuple(dict.fromkeys(v for v in venues if v))
        combos = product(names, repeat=3)
        return tuple(TriangularRoute(chain_id, (c[0], c[1], c[2]), *token_cycle) for c in combos)
