from decimal import Decimal

from src.dragon.economic_agent import ChainMarketState, EconomicDecisionAgent


class Opp:
    def __init__(self, chain_id, net, amount=10):
        self.chain_id = chain_id
        self.net_profit_quote = Decimal(str(net))
        self.quote_amount = Decimal(str(amount))


def test_agent_ranks_expected_executable_value():
    agent = EconomicDecisionAgent()
    agent.observe_execution(chain_id=8453, success=True, realized_net_quote=Decimal("0.50"), latency_ms=Decimal("100"))
    agent.observe_execution(chain_id=56, success=False, latency_ms=Decimal("800"))
    rows = agent.rank(
        [Opp(8453, "0.60"), Opp(56, "1.00")],
        {
            8453: ChainMarketState(8453, rpc_success_rate=Decimal("1"), route_success_rate=Decimal("1")),
            56: ChainMarketState(56, rpc_success_rate=Decimal("0.8"), route_success_rate=Decimal("0.8")),
        },
    )
    assert rows
    assert rows[0].chain_id == 8453


def test_agent_never_removes_healthy_discovery_chains():
    agent = EconomicDecisionAgent()
    ordered = agent.prioritize_chains([42220, 56, 8453], {8453: Decimal("20"), 56: Decimal("10")})
    assert set(ordered) == {42220, 56, 8453}


def test_agent_rejects_below_hard_profit_floor():
    agent = EconomicDecisionAgent()
    assert agent.evaluate(Opp(8453, "0.004")) is None
