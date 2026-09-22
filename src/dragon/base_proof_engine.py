from __future__ import annotations

"""Base proof gate.

This is the last off-chain proof before a real transaction simulator exists.
It re-quotes both legs against the same observed Base block and verifies that
the economic edge survives a fresh state read. It intentionally does NOT claim
to simulate token transfers, flash-loan callbacks, or mempool ordering.
"""

from dataclasses import dataclass
from decimal import Decimal
import time


@dataclass(frozen=True)
class ProofResult:
    passed: bool
    block_number: int
    first_quote_out: int
    second_quote_out: int
    final_amount: int
    net_profit_quote: Decimal
    reason: str
    execution_simulation: bool = False


class BaseProofEngine:
    def __init__(self, *, min_profit: Decimal = Decimal("0.002"), max_block_drift: int = 0):
        self.min_profit = Decimal(min_profit)
        if self.min_profit < Decimal("0.002"):
            raise ValueError("minimum profit floor cannot be below 0.002")
        self.max_block_drift = max(0, int(max_block_drift))

    def prove(self, *, adapter, opportunity, taker: str, slippage_bps: int = 50) -> ProofResult:
        chain_id = int(opportunity.chain_id)
        if chain_id != 8453:
            return ProofResult(False, 0, 0, 0, 0, Decimal("-Infinity"), "Base proof gate only", False)

        w3 = getattr(adapter, "w3", None)
        if w3 is None:
            evm = getattr(adapter, "evm", None)
            w3 = getattr(evm, "w3", None) if evm is not None else None
        if w3 is None:
            return ProofResult(False, 0, 0, 0, 0, Decimal("-Infinity"), "no Base Web3 provider", False)

        before = int(w3.eth.block_number)
        try:
            first, first_exec = adapter.quote_single_source(
                chain_id=chain_id,
                sell_token=opportunity.quote_token,
                buy_token=opportunity.base_token,
                sell_amount=int(opportunity.quote_amount),
                taker=taker,
                source=opportunity.buy_source,
                slippage_bps=slippage_bps,
                deadline=time.perf_counter() + 2.0,
                probe=True,
            )
            first_block = int(w3.eth.block_number)

            second, second_exec = adapter.quote_single_source(
                chain_id=chain_id,
                sell_token=opportunity.base_token,
                buy_token=opportunity.quote_token,
                sell_amount=int(first.buy_amount),
                taker=taker,
                source=opportunity.sell_source,
                slippage_bps=slippage_bps,
                deadline=time.perf_counter() + 2.0,
                probe=True,
            )
            after = int(w3.eth.block_number)
        except Exception as exc:
            return ProofResult(False, max(before, int(w3.eth.block_number)), 0, 0, 0, Decimal("-Infinity"), f"fresh quote failed: {type(exc).__name__}", False)

        if after - before > self.max_block_drift or after - first_block > self.max_block_drift:
            return ProofResult(False, after, int(first.buy_amount), int(second.buy_amount), int(second.buy_amount), Decimal("-Infinity"), "Base state changed during proof", False)

        final_amount = int(second.buy_amount)
        net = (
            Decimal(final_amount - int(opportunity.quote_amount))
            / (Decimal(10) ** int(getattr(opportunity, "quote_decimals", 0) or 0))
        )
        # Prefer the scanner's quote-unit economics when available.
        scanner_net = Decimal(str(getattr(opportunity, "net_profit_quote", "-Infinity")))
        if scanner_net.is_finite():
            net = min(net, scanner_net)

        passed = final_amount > int(opportunity.quote_amount) and net >= self.min_profit
        reason = "fresh same-block quote proof passed" if passed else "fresh quote proof below minimum"
        return ProofResult(passed, after, int(first.buy_amount), final_amount, final_amount, net, reason, False)
