from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class OptimizationDecision:
    """Deterministic execution-ranking result for already-validated opportunities."""

    opportunity: object
    score: Decimal
    rank_reason: str


def _dec(value: object, default: Decimal = Decimal("0")) -> Decimal:
    try:
        value = Decimal(str(value))
    except Exception:
        return default
    return value if value.is_finite() else default


def pareto_prune(
    opportunities: Iterable[T],
    *,
    profit_attr: str = "net_profit_quote",
    gas_attr: str = "gas_cost_quote",
    fee_attr: str = "flash_loan_fee_quote",
) -> list[T]:
    """Remove only candidates that are strictly dominated on execution economics.

    A candidate is dominated when another candidate has at least as much net
    profit and no worse gas/flash cost, with one strict improvement. This is
    deliberately conservative: it does not discard a route merely because it
    is larger or smaller, and it never changes an opportunity's economics.
    """
    rows = list(opportunities)
    kept: list[T] = []
    for candidate in rows:
        cp = _dec(getattr(candidate, profit_attr, 0), Decimal("-Infinity"))
        cg = _dec(getattr(candidate, gas_attr, 0), Decimal("Infinity"))
        cf = _dec(getattr(candidate, fee_attr, 0), Decimal("Infinity"))
        dominated = False
        for other in rows:
            if other is candidate:
                continue
            op = _dec(getattr(other, profit_attr, 0), Decimal("-Infinity"))
            og = _dec(getattr(other, gas_attr, 0), Decimal("Infinity"))
            of = _dec(getattr(other, fee_attr, 0), Decimal("Infinity"))
            if op >= cp and og <= cg and of <= cf and (op > cp or og < cg or of < cf):
                dominated = True
                break
        if not dominated:
            kept.append(candidate)
    return kept


def rank_opportunities(opportunities: Sequence[T]) -> list[OptimizationDecision]:
    """Rank executable candidates without inventing a risk score.

    Primary objective is realized/net-profit estimate. Tie-breakers prefer
    lower execution cost, then better profit density. The minimum-profit gate
    remains the responsibility of the execution engine/contract.
    """
    decisions: list[OptimizationDecision] = []
    for opportunity in pareto_prune(opportunities):
        profit = _dec(getattr(opportunity, "net_profit_quote", 0))
        gas = _dec(getattr(opportunity, "gas_cost_quote", 0), Decimal("Infinity"))
        fee = _dec(getattr(opportunity, "flash_loan_fee_quote", 0), Decimal("Infinity"))
        amount = _dec(getattr(opportunity, "quote_amount", 0))
        density = profit / amount if amount > 0 else Decimal("-Infinity")
        # Lexicographic sorting is performed by the caller; score is kept as
        # the primary economic objective rather than a fabricated probability.
        decisions.append(
            OptimizationDecision(
                opportunity=opportunity,
                score=profit,
                rank_reason=(
                    f"net={profit}; gas={gas}; flash_fee={fee}; "
                    f"profit_density={density}"
                ),
            )
        )
    return sorted(
        decisions,
        key=lambda d: (
            d.score,
            -_dec(getattr(d.opportunity, "gas_cost_quote", 0), Decimal("Infinity")),
            -_dec(getattr(d.opportunity, "flash_loan_fee_quote", 0), Decimal("Infinity")),
            _dec(getattr(d.opportunity, "net_profit_quote", 0))
            / max(_dec(getattr(d.opportunity, "quote_amount", 0)), Decimal("1")),
        ),
        reverse=True,
    )


def choose_best(opportunities: Sequence[T]) -> T | None:
    ranked = rank_opportunities(opportunities)
    return ranked[0].opportunity if ranked else None
