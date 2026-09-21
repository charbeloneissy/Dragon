from dataclasses import dataclass
from decimal import Decimal

from dragon.execution_optimizer import choose_best, pareto_prune


@dataclass
class O:
    net_profit_quote: Decimal
    gas_cost_quote: Decimal
    flash_loan_fee_quote: Decimal
    quote_amount: int


def test_pareto_prunes_strictly_dominated_candidate():
    good = O(Decimal("0.020"), Decimal("0.002"), Decimal("0.001"), 100)
    dominated = O(Decimal("0.010"), Decimal("0.003"), Decimal("0.002"), 100)
    assert pareto_prune([good, dominated]) == [good]


def test_pareto_keeps_tradeoff_candidates():
    higher_profit = O(Decimal("0.020"), Decimal("0.010"), Decimal("0.001"), 100)
    lower_cost = O(Decimal("0.015"), Decimal("0.001"), Decimal("0.001"), 100)
    assert len(pareto_prune([higher_profit, lower_cost])) == 2


def test_choose_best_uses_net_profit():
    first = O(Decimal("0.011"), Decimal("0.001"), Decimal("0.001"), 100)
    second = O(Decimal("0.019"), Decimal("0.004"), Decimal("0.001"), 200)
    assert choose_best([first, second]) is second
