from __future__ import annotations

import threading
import time
from decimal import Decimal
from typing import Any

from src.dragon.amplitude_telemetry import AmplitudeTelemetry


COUNTERS = (
    "pool_events_received",
    "pools_with_fresh_state",
    "opportunities_detected",
    "opportunities_after_fees",
    "opportunities_after_gas",
    "opportunities_after_slippage",
    "opportunities_after_costs",
    "optimal_size_found",
    "fresh_simulation_passed",
    "tx_submitted",
    "tx_included",
    "tx_reverted",
    "completed_arbs",
    "realized_profit",
    "realized_loss",
    "quote_observations",
    "quote_failures",
)


class ExecutionTelemetry:
    """Thread-safe counters and bounded lifecycle records for one DEX run."""

    def __init__(self, max_records: int = 100):
        self._counters = {name: 0 for name in COUNTERS}
        self._records: dict[str, dict[str, Any]] = {}
        self._max_records = max(10, int(max_records))
        self._sequence = 0
        self._lock = threading.Lock()
        self.amplitude = AmplitudeTelemetry()

    def increment(self, name: str, amount: int = 1) -> None:
        if name not in self._counters:
            return
        with self._lock:
            self._counters[name] += int(amount)

    def set_gauge(self, name: str, value: int) -> None:
        if name not in self._counters:
            return
        with self._lock:
            self._counters[name] = max(0, int(value))

    def record_opportunity(self, opportunity: Any, *, stage: str = "opportunity_after_costs") -> str:
        now = time.time()
        with self._lock:
            self._sequence += 1
            identifier = f"dex-{int(now * 1000)}-{self._sequence}"
            self._records[identifier] = {
                "id": identifier,
                "source": "base-dex",
                "stage": stage,
                "detected_at": now,
                "updated_at": now,
                "buy_source": str(getattr(opportunity, "buy_source", "")),
                "sell_source": str(getattr(opportunity, "sell_source", "")),
                "base_token": str(getattr(opportunity, "base_token", "")),
                "quote_token": str(getattr(opportunity, "quote_token", "")),
                "quote_amount": str(getattr(opportunity, "quote_amount", "")),
                "compound_amount": str(getattr(opportunity, "compound_amount", 0)),
                "flash_loan_amount": str(getattr(opportunity, "flash_loan_amount", getattr(opportunity, "quote_amount", ""))),
                "gross_profit_quote": str(getattr(opportunity, "gross_profit_quote", "")),
                "net_profit_quote": str(getattr(opportunity, "net_profit_quote", "")),
                "gas_cost_quote": str(getattr(opportunity, "gas_cost_quote", "")),
                "flash_loan_fee_quote": str(getattr(opportunity, "flash_loan_fee_quote", "")),
                "safety_buffer_quote": str(getattr(opportunity, "safety_buffer_quote", "")),
                "safety_buffer_quote": str(getattr(opportunity, "safety_buffer_quote", "")),
                "fresh_quote": True,
                "optimal_size": int(getattr(opportunity, "quote_amount", 0)) > 0,
            }
            self._trim_locked()
        self.amplitude.track("dragon_opportunity_detected", {"opportunity_id": identifier, "stage": stage, "buy_source": str(getattr(opportunity, "buy_source", "")), "sell_source": str(getattr(opportunity, "sell_source", "")), "base_token": str(getattr(opportunity, "base_token", "")), "quote_token": str(getattr(opportunity, "quote_token", "")), "gross_profit_quote": str(getattr(opportunity, "gross_profit_quote", "")), "net_profit_quote": str(getattr(opportunity, "net_profit_quote", "")), "gas_cost_quote": str(getattr(opportunity, "gas_cost_quote", "")), "flash_loan_fee_quote": str(getattr(opportunity, "flash_loan_fee_quote", "")), "compound_amount": "0"})
        return identifier

    def mark(self, identifier: str, stage: str, **fields: Any) -> None:
        if not identifier:
            return
        with self._lock:
            record = self._records.get(identifier)
            if record is None:
                return
            record.update(fields)
            record["stage"] = stage
            record["updated_at"] = time.time()
            record.setdefault("timestamps", {})[stage] = record["updated_at"]

    def mark_submission(self, identifier: str, tx_hash: str) -> None:
        self.increment("tx_submitted")
        self.mark(identifier, "submitted", tx_hash=tx_hash, submitted_at=time.time())
        self.amplitude.track("dragon_execution_submitted", {"opportunity_id": identifier, "tx_hash": tx_hash})

    def mark_included(self, identifier: str, receipt: dict[str, Any], *, realized_pnl_quote: Decimal | None = None) -> None:
        self.increment("tx_included")
        self.increment("completed_arbs")
        if realized_pnl_quote is None and receipt.get("realized_pnl_quote") is not None:
            realized_pnl_quote = Decimal(str(receipt["realized_pnl_quote"]))
        pnl = None if realized_pnl_quote is None else float(realized_pnl_quote)
        if realized_pnl_quote is not None:
            self.increment("realized_profit" if realized_pnl_quote >= 0 else "realized_loss")
        self.mark(
            identifier,
            "included",
            included_at=time.time(),
            block_number=receipt.get("blockNumber"),
            gas_used=receipt.get("gasUsed"),
            actual_profit_quote=receipt.get("arb_profit_quote"),
            actual_premium_quote=receipt.get("arb_premium_quote"),
            actual_gas_cost_native=receipt.get("gas_cost_native"),
            actual_gas_cost_quote=receipt.get("gas_cost_quote"),
            realized_pnl_quote=pnl,
            legs={"leg_1": "FILLED", "leg_2": "FILLED", "repayment": "FILLED"},
        )
        self.amplitude.track("dragon_execution_included", {"opportunity_id": identifier, "realized_pnl_quote": pnl, "gas_cost_native": receipt.get("gas_cost_native"), "gas_cost_quote": receipt.get("gas_cost_quote"), "tx_hash": receipt.get("transactionHash")})

    def mark_reverted(self, identifier: str, *, error: str, tx_hash: str | None = None, receipt: dict[str, Any] | None = None) -> None:
        self.increment("tx_reverted")
        self.increment("realized_loss")
        receipt = receipt or {}
        self.mark(
            identifier,
            "reverted",
            reverted_at=time.time(),
            tx_hash=tx_hash,
            error=error,
            block_number=receipt.get("blockNumber"),
            gas_used=receipt.get("gasUsed"),
            actual_gas_cost_native=receipt.get("gas_cost_native"),
            legs={"leg_1": "REVERTED", "leg_2": "REVERTED", "repayment": "NOT FILLED"},
        )
        self.amplitude.track("dragon_execution_reverted", {"opportunity_id": identifier, "error": error[:500], "tx_hash": tx_hash, "gas_cost_native": receipt.get("gas_cost_native")})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "recent_records": sorted(
                    (dict(record) for record in self._records.values()),
                    key=lambda record: record.get("detected_at", 0),
                    reverse=True,
                )[:50],
            }

    def _trim_locked(self) -> None:
        if len(self._records) <= self._max_records:
            return
        keep = sorted(self._records.values(), key=lambda record: record.get("detected_at", 0), reverse=True)[: self._max_records]
        self._records = {record["id"]: record for record in keep}
