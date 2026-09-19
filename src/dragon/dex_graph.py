from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class DexGraphEdge:
    """Executable, size-specific quote edge for route discovery."""
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
        # Product(rate) > 1 is equivalent to sum(-ln(rate)) < 0.
        return -math.log(float(rate))


@dataclass(frozen=True)
class DexNegativeCycle:
    edges: tuple[DexGraphEdge, ...]
    product_rate: Decimal
    log_weight: float


class DexGraph:
    """Bellman-Ford candidate discovery over executable DEX quote edges.

    Tokens are graph nodes and venue stays on each edge. This deliberately
    allows a cycle to switch venues between legs, which is required for
    cross-DEX arbitrage. The result is only a candidate: the full route must
    be re-quoted and pass Dragon's existing net-profit/latency safety gates.
    """

    def __init__(self, edges: Iterable[DexGraphEdge] = ()):
        self.edges = tuple(e for e in edges if self._valid(e))

    @staticmethod
    def _valid(edge: DexGraphEdge) -> bool:
        return (
            bool(edge.source) and bool(edge.venue)
            and bool(edge.sell_token) and bool(edge.buy_token)
            and edge.sell_amount > 0 and edge.buy_amount > 0
            and edge.rate.is_finite() and edge.rate > 0
        )

    @staticmethod
    def _node(token: str) -> str:
        return token.lower()

    def negative_cycles(self, max_cycles: int = 32) -> list[DexNegativeCycle]:
        if max_cycles <= 0 or not self.edges:
            return []

        nodes = {self._node(e.sell_token) for e in self.edges}
        nodes.update(self._node(e.buy_token) for e in self.edges)
        node_list = list(nodes)
        index = {node: i for i, node in enumerate(node_list)}

        # Add a synthetic zero-cost super-source so disconnected token
        # components are all considered.
        dist = [0.0] * len(node_list)
        predecessor: list[int | None] = [None] * len(node_list)
        predecessor_edge: list[DexGraphEdge | None] = [None] * len(node_list)
        changed_vertex: int | None = None

        for _ in range(len(node_list)):
            changed_vertex = None
            for edge in self.edges:
                u = index[self._node(edge.sell_token)]
                v = index[self._node(edge.buy_token)]
                candidate = dist[u] + edge.weight
                if candidate < dist[v] - 1e-12:
                    dist[v] = candidate
                    predecessor[v] = u
                    predecessor_edge[v] = edge
                    changed_vertex = v
            if changed_vertex is None:
                break

        if changed_vertex is None:
            return []

        cycles: list[DexNegativeCycle] = []
        seen: set[tuple[tuple[str, str, str], ...]] = set()

        # There can be multiple negative cycles. Walk predecessors from each
        # relaxed vertex until a repeated vertex is reached.
        for start in [changed_vertex] + [i for i in range(len(node_list)) if i != changed_vertex]:
            if len(cycles) >= max_cycles:
                break
            x = start
            for _ in range(len(node_list)):
                if predecessor[x] is None:
                    break
                x = predecessor[x]
            else:
                cycle_edges: list[DexGraphEdge] = []
                cur = x
                for _ in range(len(node_list) + 1):
                    edge = predecessor_edge[cur]
                    prev = predecessor[cur]
                    if edge is None or prev is None:
                        cycle_edges = []
                        break
                    cycle_edges.append(edge)
                    cur = prev
                    if cur == x:
                        break
                if cycle_edges and cur == x:
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
                    if product > Decimal("1"):
                        cycles.append(
                            DexNegativeCycle(tuple(cycle_edges), product, total)
                        )
        return cycles

    def best_cycles(self, max_cycles: int = 32) -> list[DexNegativeCycle]:
        return sorted(
            self.negative_cycles(max_cycles),
            key=lambda cycle: cycle.product_rate,
            reverse=True,
        )


def edges_from_quotes(quotes: Iterable[object]) -> list[DexGraphEdge]:
    """Convert normalized DexQuote-like objects into valid graph edges.

    Quotes should all use the same trade size for a given graph snapshot.
    Invalid, zero, non-finite, or negative-output quotes are discarded.
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
