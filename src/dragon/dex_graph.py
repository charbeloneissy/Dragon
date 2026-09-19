from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class DexGraphEdge:
    """Executable, size-specific quote edge for fast route discovery."""
    source: str
    venue: str
    sell_token: str
    buy_token: str
    sell_amount: Decimal
    buy_amount: Decimal
    fee_bps: Decimal = Decimal("0")

    @property
    def rate(self) -> Decimal:
        if self.sell_amount <= 0 or self.buy_amount <= 0:
            return Decimal("0")
        return self.buy_amount / self.sell_amount

    @property
    def weight(self) -> float:
        rate = self.rate
        if rate <= 0 or not rate.is_finite():
            return math.inf
        return -math.log(float(rate))


@dataclass(frozen=True)
class DexNegativeCycle:
    edges: tuple[DexGraphEdge, ...]
    product_rate: Decimal
    log_weight: float


class DexGraph:
    """Fast Bellman-Ford discovery over executable DEX quote edges.

    This is a discovery/prioritization layer only. Every returned cycle must
    be re-quoted through Dragon's normal execution path and pass latency,
    gas, slippage, minimum-net-profit and execution-safety gates.
    """

    def __init__(self, edges: Iterable[DexGraphEdge] = ()):
        self.edges = tuple(e for e in edges if self._valid(e))
        self._nodes = tuple({
            e.sell_token.lower() for e in self.edges
        } | {
            e.buy_token.lower() for e in self.edges
        })
        self._index = {node: i for i, node in enumerate(self._nodes)}

    @staticmethod
    def _valid(edge: DexGraphEdge) -> bool:
        return (
            bool(edge.source) and bool(edge.venue)
            and bool(edge.sell_token) and bool(edge.buy_token)
            and edge.sell_amount > 0 and edge.buy_amount > 0
            and edge.rate.is_finite() and edge.rate > 0
        )

    def negative_cycles(self, max_cycles: int = 32) -> list[DexNegativeCycle]:
        """Find negative cycles without scanning every possible start vertex.

        Uses a zero-cost super-source (all distances start at zero), so every
        connected component is considered. The predecessor walk is performed
        only for vertices relaxed on the final pass, avoiding the previous
        O(V^2) cycle-extraction scan.
        """
        if max_cycles <= 0 or not self.edges or not self._nodes:
            return []

        n = len(self._nodes)
        dist = [0.0] * n
        predecessor = [-1] * n
        predecessor_edge: list[DexGraphEdge | None] = [None] * n
        relaxed_last: list[int] = []

        # Standard Bellman-Ford negative-cycle test. Early exit when a full
        # pass makes no change. A final-pass relaxation proves a negative cycle.
        for iteration in range(n):
            changed = False
            relaxed_last = []
            for edge in self.edges:
                u = self._index[edge.sell_token.lower()]
                v = self._index[edge.buy_token.lower()]
                candidate = dist[u] + edge.weight
                if candidate < dist[v] - 1e-12:
                    dist[v] = candidate
                    predecessor[v] = u
                    predecessor_edge[v] = edge
                    changed = True
                    if iteration == n - 1:
                        relaxed_last.append(v)
            if not changed:
                return []

        cycles: list[DexNegativeCycle] = []
        seen: set[tuple[tuple[str, str, str], ...]] = set()

        for start in relaxed_last:
            if len(cycles) >= max_cycles:
                break

            x = start
            valid = True
            for _ in range(n):
                x_prev = predecessor[x]
                if x_prev < 0:
                    valid = False
                    break
                x = x_prev
            if not valid:
                continue

            cycle_edges: list[DexGraphEdge] = []
            cur = x
            for _ in range(n + 1):
                edge = predecessor_edge[cur]
                prev = predecessor[cur]
                if edge is None or prev < 0:
                    cycle_edges = []
                    break
                cycle_edges.append(edge)
                cur = prev
                if cur == x:
                    break

            if not cycle_edges or cur != x:
                continue

            cycle_edges.reverse()
            key = tuple(
                (e.venue, e.sell_token.lower(), e.buy_token.lower())
                for e in cycle_edges
            )
            if key in seen:
                continue
            seen.add(key)

            product = Decimal("1")
            total = 0.0
            for edge in cycle_edges:
                product *= edge.rate
                total += edge.weight

            # Final guard against numerical noise.
            if product > Decimal("1") and total < -1e-12:
                cycles.append(DexNegativeCycle(tuple(cycle_edges), product, total))

        return cycles

    def best_cycles(self, max_cycles: int = 32) -> list[DexNegativeCycle]:
        return sorted(
            self.negative_cycles(max_cycles),
            key=lambda cycle: cycle.product_rate,
            reverse=True,
        )


def edges_from_quotes(quotes: Iterable[object]) -> list[DexGraphEdge]:
    """Convert normalized DexQuote-like objects into valid graph edges.

    Quotes must represent the same trade-size snapshot. Invalid, zero,
    non-finite or negative-output quotes are discarded.
    """
    edges: list[DexGraphEdge] = []
    for quote in quotes:
        try:
            sell = Decimal(str(getattr(quote, "sell_amount")))
            buy = Decimal(str(getattr(quote, "buy_amount")))
            edge = DexGraphEdge(
                source=str(getattr(quote, "venue")),
                venue=str(getattr(quote, "venue")),
                sell_token=str(getattr(quote, "sell_token")),
                buy_token=str(getattr(quote, "buy_token")),
                sell_amount=sell,
                buy_amount=buy,
                fee_bps=Decimal(str(getattr(quote, "fee_bps", 0))),
            )
            if DexGraph._valid(edge):
                edges.append(edge)
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            continue
    return edges
