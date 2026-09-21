from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Iterable

from .contracts import LiquidityState, Opportunity


@dataclass(frozen=True)
class SizePoint:
    amount: Decimal
    net: Decimal
    expected_realized_net: Decimal


@dataclass(frozen=True)
class OptimalSize:
    amount: Decimal
    expected_realized_net: Decimal
    curve: tuple[SizePoint, ...]


class DynamicSizer:
    """Find Q* without assuming that profit grows linearly with size.

    The route quote callback must re-price every candidate amount.  Dragon never
    extrapolates an old quote into a new trade size.
    """

    def __init__(self, *, min_profit: Decimal = Decimal("0.005")) -> None:
        self.min_profit = Decimal(min_profit)
        if self.min_profit < Decimal("0.005"):
            raise ValueError("minimum profit cannot be below $0.005")

    def optimize(
        self,
        *,
        liquidity: LiquidityState,
        seed_amounts: Iterable[Decimal],
        quote: Callable[[Decimal], Opportunity | None],
    ) -> OptimalSize | None:
        capacity = liquidity.executable_capacity
        candidates = sorted({Decimal(x) for x in seed_amounts if Decimal(x) > 0 and Decimal(x) <= capacity})
        points: list[SizePoint] = []

        for amount in candidates:
            opportunity = quote(amount)
            if opportunity is None:
                continue
            points.append(
                SizePoint(
                    amount=amount,
                    net=opportunity.deterministic_net,
                    expected_realized_net=opportunity.expected_realized_net,
                )
            )

        if not points:
            return None

        best = max(points, key=lambda p: (p.expected_realized_net, p.net, -p.amount))
        if best.expected_realized_net < self.min_profit:
            return None

        return OptimalSize(
            amount=best.amount,
            expected_realized_net=best.expected_realized_net,
            curve=tuple(points),
        )
