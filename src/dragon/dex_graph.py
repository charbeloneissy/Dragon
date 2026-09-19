from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class DexGraphEdge:
    """Executable, size-specific swap edge used only for route discovery.

    rate is expressed in human token units after the venue's quoted execution
    price. Venue is part of the edge identity so discovery never collapses
    quotes from different DEXes into one synthetic pool.
    """
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
        # Negative log turns multiplicative arbitrage into additive
        # negative-cycle detection: product(rate) > 1 <=> sum(-ln(rate)) < 0.
        return -math.log(float(rate))


@dataclass(frozen=True)
class DexNegativeCycle:
    edges: tuple[DexGraphEdge, ...]
    product_rate: Decimal
    log_weight: float


class DexGraph:
    """Small, venue-aware Bellman-Ford graph for DEX route discovery.

    This module discovers candidates only. It does not declare a trade
    profitable: callers must re-quote the full path and apply gas, flash-fee,
    safety-buffer, latency and minimum-net-profit gates before execution.
    """

    def __init__(self, edges: Iterable[DexGraphEdge] = ()):
        self.edges = tuple(e for e in edges if self._valid(e))

    @staticmethod
    def _valid(edge: DexGraphEdge) -> bool:
        return (
            bool(edge.source)
            and bool(edge.venue)
            and bool(edge.sell_token)
            and bool(edge.buy_token)
            and edge.sell_amount > 0
            and edge.buy_amount > 0
            and edge.rate.is_finite()
            and edge.rate > 0
        )

    @staticmethod
    def _node(edge: DexGraphEdge) -> tuple[str, str]:
        # Venue is retained in the node identity. A route cannot silently
        # switch DEXes without an explicit cross-venue edge.
        return edge.venue, edge.sell_token.lower()

    @staticmethod
    def _to_node(edge: DexGraphEdge) -> tuple[str, str]:
        return edge.venue, edge.buy_token.lower()

    def negative_cycles(self, max_cycles: int = 32) -> list[DexNegativeCycle]:
        if max_cycles <= 0 or not self.edges:
            return []

        # Bellman-Ford over (venue, token) nodes. For a cycle to represent an
        # executable cross-DEX route, venues must be connected by explicit
        # edges supplied by the caller. We therefore do not fabricate venue
        # switches from a token appearing on multiple DEXes.
        nodes = set()
        for edge in self.edges:
            nodes.add(self._node(edge))
            nodes.add(self._to_node(edge))
        node_list = list(nodes)
        index = {node: i for i, node in enumerate(node_list)}
        source = 0
        dist = [math.inf] * len(node_list)
        dist[source] = 0.0
        predecessor: list[int | None] = [None] * len(node_list)
        predecessor_edge: list[DexGraphEdge | None] = [None] * len(node_list)

        for _ in range(max(0, len(node_list) - 1)):
            changed = False
            for edge in self.edges:
                u = index[self._node(edge)]
                v = index[self._to_node(edge)]
                if dist[u] == math.inf:
                    continue
                candidate = dist[u] + edge.weight
                if candidate < dist[v] - 1e-12:
                    dist[v] = candidate
                    predecessor[v] = u
                    predecessor_edge[v] = edge
                    changed = True
            if not changed:
                break

        cycles: list[DexNegativeCycle] = []
        seen: set[tuple[tuple[str, str], ...]] = set()
        for edge in self.edges:
            u = index[self._node(edge)]
            v = index[self._to_node(edge)]
            if dist[u] == math.inf or dist[u] + edge.weight >= dist[v] - 1e-12:
                continue

            x = v
            for _ in range(len(node_list)):
                if predecessor[x] is None:
                    break
                x = predecessor[x]
            else:
                cycle_edges: list[DexGraphEdge] = []
                cur = x
                for _ in range(len(node_list) + 1):
                    pe = predecessor_edge[cur]
                    if pe is None:
                        cycle_edges = []
                        break
                    cycle_edges.append(pe)
                    cur = predecessor[cur]  # type: ignore[index]
                    if cur == x:
                        break

                if cycle_edges and cur == x:
                    cycle_edges.reverse()
                    key = tuple((e.venue, e.sell_token.lower(), e.buy_token.lower()) for e in cycle_edges)
                    if key in seen:
                        continue
                    seen.add(key)
                    product = Decimal("1")
                    total = 0.0
                    for ce in cycle_edges:
                        product *= ce.rate
                        total += ce.weight
                    if product > Decimal("1"):
                        cycles.append(DexNegativeCycle(tuple(cycle_edges), product, total))
                        if len(cycles) >= max_cycles:
                            break
        return cycles

    def best_cycles(self, max_cycles: int = 32) -> list[DexNegativeCycle]:
        return sorted(
            self.negative_cycles(max_cycles=max_cycles),
            key=lambda cycle: cycle.product_rate,
            reverse=True,
        )


def edges_from_quotes(quotes: Iterable[object], *, venue_attr: str = "venue") -> list[DexGraphEdge]:
    """Convert normalized DexQuote-like objects into graph edges.

    Quotes must already be executable quotes for the same trade size. Invalid,
    zero, non-finite, or negative-output quotes are ignored.
    """
    edges: list[DexGraphEdge] = []
    for quote in quotes:
        try:
            sell = Decimal(str(getattr(quote, "sell_amount")))
            buy = Decimal(str(getattr(quote, "buy_amount")))
            fee = Decimal(str(getattr(quote, "fee_bps", 0)))
            venue = str(getattr(quote, venue_attr))
            source = str(getattr(quote, "venue", venue))
            edge = DexGraphEdge(
                source=source,
                venue=venue,
                sell_token=str(getattr(quote, "sell_token")),
                buy_token=str(getattr(quote, "buy_token")),
                sell_amount=sell,
                buy_amount=buy,
                fee_bps=fee,
            )
            if DexGraph._valid(edge):
                edges.append(edge)
        except (AttributeError, TypeError, ValueError, ArithmeticError):
            continue
    return edges
