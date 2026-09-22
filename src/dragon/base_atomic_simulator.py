from __future__ import annotations

"""Exact Base atomic-call simulator; fail-closed until an executor is deployed."""

from dataclasses import dataclass
from decimal import Decimal
import os
import time


@dataclass(frozen=True)
class AtomicSimulationResult:
    passed: bool
    block_number: int
    target: str
    gas_estimate: int
    reason: str
    first_amount_out: int = 0
    second_amount_out: int = 0
    execution_simulation: bool = True


class BaseAtomicSimulator:
    def __init__(self, *, min_profit: Decimal = Decimal("0.002")):
        self.min_profit = Decimal(min_profit)
        if self.min_profit < Decimal("0.002"):
            raise ValueError("minimum profit floor cannot be below 0.002")

    def simulate(self, *, adapter, opportunity, taker: str, slippage_bps: int = 50) -> AtomicSimulationResult:
        if int(opportunity.chain_id) != 8453:
            return AtomicSimulationResult(False, 0, "", 0, "Base simulator only")
        executor = os.getenv("DRAGON_ATOMIC_EXECUTOR", "").strip()
        if not executor:
            return AtomicSimulationResult(False, 0, "", 0, "DRAGON_ATOMIC_EXECUTOR not configured")
        w3 = getattr(adapter, "w3", None) or getattr(getattr(adapter, "evm", None), "w3", None)
        if w3 is None:
            return AtomicSimulationResult(False, 0, executor, 0, "no Base Web3 provider")
        try:
            executor = w3.to_checksum_address(executor)
            caller = w3.to_checksum_address(os.getenv("DRAGON_ATOMIC_OWNER", taker))
            first, first_exec = adapter.quote_single_source(
                chain_id=8453, sell_token=opportunity.quote_token, buy_token=opportunity.base_token,
                sell_amount=int(opportunity.quote_amount), taker=executor, source=opportunity.buy_source,
                slippage_bps=slippage_bps, deadline=time.perf_counter()+2.0, probe=False)
            second, second_exec = adapter.quote_single_source(
                chain_id=8453, sell_token=opportunity.base_token, buy_token=opportunity.quote_token,
                sell_amount=int(first.buy_amount), taker=executor, source=opportunity.sell_source,
                slippage_bps=slippage_bps, deadline=time.perf_counter()+2.0, probe=False)
            abi=[{"inputs":[
                {"internalType":"address","name":"asset","type":"address"},
                {"internalType":"uint256","name":"amount","type":"uint256"},
                {"components":[
                    {"internalType":"address","name":"target","type":"address"},
                    {"internalType":"address","name":"tokenIn","type":"address"},
                    {"internalType":"address","name":"tokenOut","type":"address"},
                    {"internalType":"uint256","name":"amountIn","type":"uint256"},
                    {"internalType":"uint256","name":"minAmountOut","type":"uint256"},
                    {"internalType":"bytes","name":"data","type":"bytes"}],
                 "internalType":"struct DragonAtomicExecutor.Leg","name":"first","type":"tuple"},
                {"components":[
                    {"internalType":"address","name":"target","type":"address"},
                    {"internalType":"address","name":"tokenIn","type":"address"},
                    {"internalType":"address","name":"tokenOut","type":"address"},
                    {"internalType":"uint256","name":"amountIn","type":"uint256"},
                    {"internalType":"uint256","name":"minAmountOut","type":"uint256"},
                    {"internalType":"bytes","name":"data","type":"bytes"}],
                 "internalType":"struct DragonAtomicExecutor.Leg","name":"second","type":"tuple"}],
                "name":"startFlashLoan","outputs":[],"stateMutability":"nonpayable","type":"function"}]
            c=w3.eth.contract(address=executor,abi=abi)
            fl=(w3.to_checksum_address(first_exec.target),w3.to_checksum_address(first_exec.token_in),
                w3.to_checksum_address(first_exec.token_out),int(first_exec.amount_in),int(first.buy_amount),first_exec.data)
            sl=(w3.to_checksum_address(second_exec.target),w3.to_checksum_address(second_exec.token_in),
                w3.to_checksum_address(second_exec.token_out),int(second_exec.amount_in),int(second.buy_amount),second_exec.data)
            tx=c.functions.startFlashLoan(w3.to_checksum_address(opportunity.quote_token),int(opportunity.quote_amount),fl,sl).build_transaction({
                "from":caller,"gas":8_000_000,"gasPrice":int(w3.eth.gas_price),"nonce":0})
            block=int(w3.eth.block_number)
            gas=int(w3.eth.estimate_gas(tx))
            w3.eth.call(tx)
            return AtomicSimulationResult(True,block,executor,gas,
                "atomic flash-loan -> DEX A -> DEX B -> repayment eth_call passed",
                int(first.buy_amount),int(second.buy_amount))
        except Exception as exc:
            return AtomicSimulationResult(False,int(w3.eth.block_number),executor,0,
                f"atomic eth_call failed: {type(exc).__name__}: {str(exc)[:180]}")
