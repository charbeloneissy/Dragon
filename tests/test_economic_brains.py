from decimal import Decimal

from src.dragon.calculator_brain import (
    ChainEconomics,
    EconomicCalculator,
    FlashLiquidity,
    RouteEconomics,
)
from src.dragon.market_brain import MarketBrain, MarketObservation


def test_calculator_accounts_for_all_costs():
    calc = EconomicCalculator()
    chain = ChainEconomics(8453, Decimal("0.00001"), Decimal("3000"))
    flash = FlashLiquidity(8453, "USDC", Decimal("100"), Decimal("5"))
    route = RouteEconomics(
        input_quote=Decimal("50"),
        final_quote=Decimal("50.50"),
        dex_fees_quote=Decimal("0.10"),
        slippage_quote=Decimal("0.05"),
        gas_quote=Decimal("0"),
        flash_fee_quote=Decimal("0"),
        sponsor_cost_quote=Decimal("0.01"),
        mev_cost_quote=Decimal("0.02"),
    )
    result = calc.calculate(flash=flash, route=route, chain=chain)
    assert result.net_profit_quote == Decimal("0.295")
    assert result.executable


def test_market_brain_does_not_hide_survival_cost():
    brain = MarketBrain()
    obs = MarketObservation(
        chain_id=8453,
        venue_buy="A",
        venue_sell="B",
        quote_token="USDC",
        base_token="WETH",
        input_amount_quote=Decimal("100"),
        output_amount_base=Decimal("1"),
        return_amount_quote=Decimal("101"),
        quote_age_ms=Decimal("100"),
        route_latency_ms=Decimal("100"),
        gas_cost_quote=Decimal("0.10"),
        dex_fees_quote=Decimal("0.10"),
        flash_fee_quote=Decimal("0.01"),
        slippage_quote=Decimal("0.05"),
        spread_survival_ms=Decimal("1000"),
        execution_window_ms=Decimal("1000"),
        liquidity_utilization=Decimal("0.2"),
        rpc_success_rate=Decimal("1"),
        route_success_rate=Decimal("1"),
    )
    decision = brain.evaluate(obs)
    assert decision.gross_profit_quote == Decimal("1")
    assert decision.total_cost_quote == Decimal("0.26")
    assert decision.executable_economic_value < Decimal("0.74")


def test_calculator_chooses_dynamic_size_and_flash_asset():
    calc = EconomicCalculator()

    def builder(flash, chain, amount):
        # Larger size has more gross profit but nonlinear slippage.
        slippage = (amount / Decimal("10")) ** 2 / Decimal("100")
        return RouteEconomics(
            input_quote=amount,
            final_quote=amount + Decimal("0.04") * amount - slippage,
            dex_fees_quote=amount * Decimal("0.001"),
            slippage_quote=slippage,
            gas_quote=Decimal("0.001"),
            flash_fee_quote=Decimal("0"),
            sponsor_cost_quote=Decimal("0"),
        )

    flashes = [
        FlashLiquidity(8453, "ASSET_A", Decimal("100"), Decimal("10")),
        FlashLiquidity(8453, "ASSET_B", Decimal("100"), Decimal("1")),
    ]
    chains = [ChainEconomics(8453, Decimal("0.000001"), Decimal("3000"))]
    result = calc.optimize(
        flashes=flashes,
        chains=chains,
        route_builder=builder,
        candidate_amounts=[Decimal("1"), Decimal("2"), Decimal("5"), Decimal("10")],
    )
    assert result is not None
    assert result.executable
    assert result.flash_asset == "ASSET_B"
    assert result.amount_quote == Decimal("5")
