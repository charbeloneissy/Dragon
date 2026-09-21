from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from web3 import Web3


AAVE_FLASH_LOAN_SIMPLE_ABI = [{
    "inputs": [
        {"internalType": "address", "name": "receiverAddress", "type": "address"},
        {"internalType": "address", "name": "asset", "type": "address"},
        {"internalType": "uint256", "name": "amount", "type": "uint256"},
        {"internalType": "bytes", "name": "params", "type": "bytes"},
        {"internalType": "uint16", "name": "referralCode", "type": "uint16"},
    ],
    "name": "flashLoanSimple",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function",
}]

FLASH_EXECUTOR_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {
                "components": [
                    {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
                    {
                        "components": [
                            {"internalType": "address", "name": "token", "type": "address"},
                            {"internalType": "address", "name": "spender", "type": "address"},
                            {"internalType": "uint256", "name": "amount", "type": "uint256"},
                        ],
                        "internalType": "struct DragonAaveFlashExecutor.Approval[]",
                        "name": "approvals",
                        "type": "tuple[]",
                    },
                    {
                        "components": [
                            {"internalType": "address", "name": "target", "type": "address"},
                            {"internalType": "uint256", "name": "value", "type": "uint256"},
                            {"internalType": "bytes", "name": "data", "type": "bytes"},
                        ],
                        "internalType": "struct DragonAaveFlashExecutor.Call[]",
                        "name": "calls",
                        "type": "tuple[]",
                    },
                    {
                        "components": [
                            {"internalType": "address", "name": "spoke", "type": "address"},
                            {"internalType": "uint256", "name": "reserveId", "type": "uint256"},
                            {"internalType": "uint16", "name": "bps", "type": "uint16"},
                        ],
                        "internalType": "struct DragonAaveFlashExecutor.YieldPlan",
                        "name": "yieldPlan",
                        "type": "tuple",
                    },
                ],
                "internalType": "struct DragonAaveFlashExecutor.FlashPlan",
                "name": "plan",
                "type": "tuple",
            },
        ],
        "name": "startFlashLoan",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


@dataclass(frozen=True)
class FlashLoanConfig:
    enabled: bool
    pool: str
    executor: str
    owner: str
    min_profit: Decimal
    max_amount: Decimal


def env_flash_loan_config() -> FlashLoanConfig:
    enabled = os.getenv("FLASH_LOAN_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    pool = os.getenv("DEX_AAVE_POOL_ADDRESS", "").strip()
    executor = os.getenv("DEX_EXECUTOR_ADDRESS", "").strip()
    owner = os.getenv("DEX_EXECUTOR_OWNER_ADDRESS", "").strip()
    min_profit = Decimal(os.getenv("DEX_MIN_NET_PROFIT", "0.005"))
    max_amount = Decimal(os.getenv("DEX_FLASH_LOAN_LIQUIDITY_QUOTE", "1000"))
    return FlashLoanConfig(enabled, pool, executor, owner, min_profit, max_amount)


def validate_config(config: FlashLoanConfig) -> None:
    if not config.enabled:
        return
    for name, value in (
        ("DEX_AAVE_POOL_ADDRESS", config.pool),
        ("DEX_EXECUTOR_ADDRESS", config.executor),
        ("DEX_EXECUTOR_OWNER_ADDRESS", config.owner),
    ):
        if not Web3.is_address(value):
            raise ValueError(f"{name} must be a valid EVM address when FLASH_LOAN_ENABLED=true")
    if config.min_profit < Decimal("0.005"):
        raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.005")
    if config.max_amount <= 0:
        raise ValueError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE must be positive")


def encode_flash_loan(
    w3: Web3,
    *,
    executor: str,
    asset: str,
    amount: int,
    min_profit: int,
    approvals: Iterable[tuple[str, str, int]],
    calls: Iterable[tuple[str, int, bytes]],
    yield_spoke: str | None = None,
    yield_reserve_id: int = 0,
    yield_bps: int = 0,
):
    """Build unsigned Dragon -> Aave flashLoanSimple calldata.

    The executor contract enforces target allowlisting and atomic repayment.
    This function only encodes; it never signs or broadcasts a transaction.
    """
    executor_contract = w3.eth.contract(
        address=Web3.to_checksum_address(executor),
        abi=FLASH_EXECUTOR_ABI,
    )
    approval_rows = [
        (Web3.to_checksum_address(token), Web3.to_checksum_address(spender), int(approval_amount))
        for token, spender, approval_amount in approvals
    ]
    call_rows = [
        (Web3.to_checksum_address(target), int(value), data)
        for target, value, data in calls
    ]
    if yield_bps < 0 or yield_bps > 10_000:
        raise ValueError("yield_bps must be between 0 and 10000")
    if yield_bps and not yield_spoke:
        raise ValueError("yield_spoke is required when yield_bps is non-zero")
    yield_plan = (
        Web3.to_checksum_address(yield_spoke) if yield_spoke else "0x0000000000000000000000000000000000000000",
        int(yield_reserve_id),
        int(yield_bps),
    )
    plan = (int(min_profit), approval_rows, call_rows, yield_plan)
    return executor_contract.functions.startFlashLoan(
        Web3.to_checksum_address(asset), int(amount), plan
    ).build_transaction({"from": Web3.to_checksum_address(os.getenv("DEX_EXECUTOR_OWNER_ADDRESS", executor))})


def aave_pool_contract(w3: Web3, pool: str):
    return w3.eth.contract(
        address=Web3.to_checksum_address(pool),
        abi=AAVE_FLASH_LOAN_SIMPLE_ABI,
    )
