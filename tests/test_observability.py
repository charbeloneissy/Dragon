from decimal import Decimal

from src.dragon.observability import ExecutionTelemetry


class Opportunity:
    buy_source = "Uniswap_V3"
    sell_source = "Aerodrome"
    base_token = "BASE"
    quote_token = "QUOTE"
    quote_amount = 100_000
    gross_profit_quote = Decimal("0.20")
    net_profit_quote = Decimal("0.10")
    gas_cost_quote = Decimal("0.03")
    flash_loan_fee_quote = Decimal("0.01")
    safety_buffer_quote = Decimal("0.01")


def test_execution_telemetry_keeps_stage_counters_and_record():
    telemetry = ExecutionTelemetry()
    telemetry.increment("opportunities_detected")
    telemetry.set_gauge("pools_with_fresh_state", 2)
    identifier = telemetry.record_opportunity(Opportunity())
    telemetry.mark(identifier, "simulation_passed")
    telemetry.mark_submission(identifier, "0xabc")

    snapshot = telemetry.snapshot()
    assert snapshot["counters"]["opportunities_detected"] == 1
    assert snapshot["counters"]["pools_with_fresh_state"] == 2
    assert snapshot["counters"]["tx_submitted"] == 1
    assert snapshot["recent_records"][0]["stage"] == "submitted"
    assert snapshot["recent_records"][0]["tx_hash"] == "0xabc"


def test_reverted_transaction_is_not_counted_as_completed():
    telemetry = ExecutionTelemetry()
    identifier = telemetry.record_opportunity(Opportunity())
    telemetry.mark_reverted(identifier, error="reverted", tx_hash="0xdead")

    counters = telemetry.snapshot()["counters"]
    assert counters["tx_reverted"] == 1
    assert counters["completed_arbs"] == 0
    assert counters["realized_loss"] == 1
