from decimal import Decimal

from src.dragon.architecture.dynamic_control import DynamicController
from src.dragon.architecture.optimizer import DynamicSizer
from src.dragon.architecture.scope import is_launch_chain


def test_launch_scope_is_three_chains():
    assert is_launch_chain(8453)
    assert is_launch_chain(56)
    assert is_launch_chain(42220)
    assert not is_launch_chain(1)


def test_dynamic_budget_adapts():
    controller = DynamicController()
    quiet = controller.update(
        opportunity_density=Decimal("0.01"),
        volatility_bps=Decimal("20"),
        rpc_latency_ms=100,
        simulation_queue=0,
    )
    active = controller.update(
        opportunity_density=Decimal("0.60"),
        volatility_bps=400,
        rpc_latency_ms=100,
        simulation_queue=0,
    )
    assert quiet.candidate_budget == 10
    assert active.candidate_budget == 100
    assert active.scan_interval_ms < quiet.scan_interval_ms


def test_dynamic_sizer_uses_expected_realized_net():
    from src.dragon.architecture.contracts import LiquidityState, Opportunity

    liquidity = LiquidityState(
        chain_id=8453,
        asset="USDC",
        flash_available=Decimal("10000"),
        flash_fee_bps=5,
        route_capacity=Decimal("10000"),
        dex_capacity=Decimal("10000"),
        observed_at_ms=1,
        block_number=1,
    )

    curve = {
        Decimal("100"): Decimal("0.01"),
        Decimal("500"): Decimal("0.04"),
        Decimal("1000"): Decimal("0.07"),
        Decimal("2000"): Decimal("0.06"),
    }

    def quote(amount):
        net = curve[amount]
        return Opportunity(
            opportunity_id=str(amount),
            chain_id=8453,
            venue_path=("aerodrome", "dex-b"),
            token_path=("USDC", "WETH", "USDC"),
            amount_in=amount,
            expected_out=amount + net,
            gross_profit=net,
            dex_fees=Decimal("0"),
            flash_fee=Decimal("0"),
            gas=Decimal("0"),
            slippage=Decimal("0"),
            price_impact=Decimal("0"),
            quote_age_ms=10,
            execution_latency_ms=20,
            execution_probability=Decimal("1"),
            state_stability=Decimal("1"),
        )

    result = DynamicSizer().optimize(
        liquidity=liquidity,
        seed_amounts=curve.keys(),
        quote=quote,
    )
    assert result is not None
    assert result.amount == Decimal("1000")
