from decimal import Decimal

from src.dragon.research_architecture import (
    TimeSeries,
    empirical_probability,
    expected_value,
    hard_gate_map,
    latency_penalty_bps,
    measure_leg,
    opportunity_score,
    spread_stats,
    tier,
    volatility_stats,
)
from src.dragon.engine_guard import EngineHealth, build_system_health, validate_quant_inputs


def test_measure_leg_buy_uses_depth_and_fee():
    book = {"asks": [["100", "0.05"], ["101", "0.05"]], "bids": [["99", "0.1"]]}
    leg = measure_leg("AAAUSDT", "buy", Decimal("7.5"), book, Decimal("10"))
    assert leg.vwap > Decimal("100")
    assert leg.output_base > Decimal("0")
    assert leg.fee_quote == Decimal("0.0075")
    assert leg.slippage_bps > Decimal("0")


def test_probability_is_smoothed():
    assert empirical_probability(0, 0) == Decimal("0.5")
    assert empirical_probability(9, 1) == Decimal("0.8333333333333333333333333333")


def test_expected_value():
    assert expected_value(Decimal("0.75"), Decimal("0.20"), Decimal("0.10")) == Decimal("0.125")


def test_latency_penalty_increases_with_latency():
    low = latency_penalty_bps(Decimal("0.001"), Decimal("10"))
    high = latency_penalty_bps(Decimal("0.001"), Decimal("100"))
    assert high > low > 0


def test_spread_and_volatility_windows():
    spreads = TimeSeries()
    prices = TimeSeries()
    for i, (spread, price) in enumerate([(5, 100), (6, 101), (4, 100), (7, 102)]):
        spreads.add(float(i), Decimal(str(spread)))
        prices.add(float(i), Decimal(str(price)))
    stats = spread_stats(spreads, 30)
    assert stats.count == 4
    assert stats.max_bps == Decimal("7")
    assert stats.min_bps == Decimal("4")
    vol = volatility_stats(prices)
    assert vol.count == 4
    assert vol.vol_1s >= 0


def test_score_and_tiers():
    score = opportunity_score(Decimal("12"), Decimal("0.10"), Decimal("1"), Decimal("0"), Decimal("0"))
    assert Decimal("0") <= score <= Decimal("100")
    assert tier(Decimal("95")) == "A+"
    assert tier(Decimal("82")) == "A"
    assert tier(Decimal("72")) == "B"
    assert tier(Decimal("62")) == "C"
    assert tier(Decimal("59")) == "REJECT"


def test_hard_gates_are_independent():
    gates = hard_gate_map(data_fresh=True, synchronized=False, sufficient_depth=True, net_edge_positive=True, expected_profit_positive=True, risk_ok=True)
    assert gates["fresh_data"] is True
    assert gates["synchronized"] is False


def test_engine_guard_requires_all_enabled_engines_healthy():
    health = build_system_health(
        market=EngineHealth("market", True, True),
        quant=EngineHealth("quant", True, True),
        risk=EngineHealth("risk", True, False, "risk feed unavailable"),
        telemetry=EngineHealth("telemetry", True, True),
        research=EngineHealth("research", True, True),
    )
    assert health.healthy is False
    assert health.degraded is True


def test_quant_input_gates_are_independent():
    gates = validate_quant_inputs(
        net_edge_bps=Decimal("8"), expected_pnl_usdt=Decimal("0.01"),
        liquidity_utilization=Decimal("0.20"), latency_penalty_bps=Decimal("1"),
        max_liquidity_utilization=Decimal("0.25"), max_latency_penalty_bps=Decimal("5"),
    )
    assert all(gates.values())
