from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3
from web3.contract import Contract


EXECUTOR_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {
                "components": [
                    {"internalType": "address", "name": "owner", "type": "address"},
                    {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
                    {"internalType": "uint256", "name": "maxBlockNumber", "type": "uint256"},
                    {
                        "components": [
                            {"internalType": "address", "name": "target", "type": "address"},
                            {"internalType": "bytes", "name": "data", "type": "bytes"},
                            {"internalType": "address", "name": "sellToken", "type": "address"},
                            {"internalType": "address", "name": "buyToken", "type": "address"},
                            {"internalType": "address", "name": "allowanceTarget", "type": "address"},
                            {"internalType": "uint256", "name": "sellAmount", "type": "uint256"},
                            {"internalType": "uint256", "name": "minBuyAmount", "type": "uint256"},
                        ],
                        "internalType": "struct DragonAaveV3Executor.Call",
                        "name": "first",
                        "type": "tuple",
                    },
                    {
                        "components": [
                            {"internalType": "address", "name": "target", "type": "address"},
                            {"internalType": "bytes", "name": "data", "type": "bytes"},
                            {"internalType": "address", "name": "sellToken", "type": "address"},
                            {"internalType": "address", "name": "buyToken", "type": "address"},
                            {"internalType": "address", "name": "allowanceTarget", "type": "address"},
                            {"internalType": "uint256", "name": "sellAmount", "type": "uint256"},
                            {"internalType": "uint256", "name": "minBuyAmount", "type": "uint256"},
                        ],
                        "internalType": "struct DragonAaveV3Executor.Call",
                        "name": "second",
                        "type": "tuple",
                    },
                ],
                "internalType": "struct DragonAaveV3Executor.FlashParams",
                "name": "params",
                "type": "tuple",
            },
        ],
        "name": "executeFlashArbitrage",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


@dataclass(frozen=True)
class FlashExecutorConfig:
    rpc_url: str
    private_key: str
    executor_address: str
    owner_address: str
    chain_id: int
    gas_limit: int | None = None
    max_fee_multiplier: Decimal = Decimal("1.20")
    max_priority_fee_gwei: Decimal = Decimal("0.001")
    mev_required: bool = True

    @classmethod
    def from_env(cls) -> "FlashExecutorConfig":
        rpc = os.getenv("DEX_PRIVATE_RPC_URL", "").strip()
        key = os.getenv("DEX_EXECUTOR_OWNER_PRIVATE_KEY", "").strip()
        executor = os.getenv("DEX_EXECUTOR_ADDRESS", "").strip()
        owner = os.getenv("DEX_EXECUTOR_OWNER_ADDRESS", "").strip()
        if not rpc or not key or not executor or not owner:
            raise RuntimeError(
                "live flash execution requires DEX_PRIVATE_RPC_URL, "
                "DEX_EXECUTOR_OWNER_PRIVATE_KEY, DEX_EXECUTOR_OWNER_ADDRESS and DEX_EXECUTOR_ADDRESS"
            )
        chain_id = int(os.getenv("DEX_CHAIN_ID", "8453"))
        if chain_id <= 0:
            raise ValueError("DEX_CHAIN_ID must be positive")
        return cls(
            rpc_url=rpc,
            private_key=key,
            executor_address=Web3.to_checksum_address(executor),
            owner_address=Web3.to_checksum_address(owner),
            chain_id=chain_id,
            gas_limit=int(os.getenv("DEX_EXECUTOR_GAS_LIMIT", "0")) or None,
            max_fee_multiplier=Decimal(os.getenv("DEX_MAX_FEE_MULTIPLIER", "1.20")),
            max_priority_fee_gwei=Decimal(os.getenv("DEX_MAX_PRIORITY_FEE_GWEI", "0.001")),
            mev_required=os.getenv("MEV_PROTECTION_REQUIRED", "true").lower() in {"1", "true", "yes", "on"},
        )


class AaveFlashExecutor:
    """Signs and broadcasts the already-quoted atomic flash-loan transaction.

    A private RPC endpoint is mandatory when MEV protection is required. The
    executor never sends to a public RPC in that mode.
    """

    def __init__(self, config: FlashExecutorConfig | None = None):
        self.config = config or FlashExecutorConfig.from_env()
        if self.config.mev_required and not os.getenv("DEX_PRIVATE_RPC_URL", "").strip():
            raise RuntimeError("MEV protection requires a private RPC endpoint")
        self.w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 10}))
        if not self.w3.is_connected():
            raise RuntimeError("cannot connect to configured private RPC")
        if self.w3.eth.chain_id != self.config.chain_id:
            raise RuntimeError("configured chain ID does not match private RPC")
        self.contract: Contract = self.w3.eth.contract(
            address=self.config.executor_address,
            abi=EXECUTOR_ABI,
        )
        self.account = self.w3.eth.account.from_key(self.config.private_key)
        if self.account.address != self.config.owner_address:
            raise RuntimeError("executor private key does not match DEX_EXECUTOR_OWNER_ADDRESS")

    @staticmethod
    def _call(execution: Any) -> tuple[Any, ...]:
        return (
            Web3.to_checksum_address(execution.to),
            execution.data,
            Web3.to_checksum_address(execution.sell_token),
            Web3.to_checksum_address(execution.buy_token),
            Web3.to_checksum_address(execution.allowance_target),
            int(execution.sell_amount),
            int(execution.buy_amount),
        )

    def build_and_send(self, opportunity: Any, *, max_block_number: int) -> str:
        if opportunity.quote_amount <= 0:
            raise ValueError("flash amount must be positive")
        if opportunity.net_profit_quote < Decimal("0.005"):
            raise ValueError("opportunity is below minimum net profit")
        if max_block_number < self.w3.eth.block_number:
            raise ValueError("max_block_number is already expired")
        if opportunity.first_leg.value != 0 or opportunity.second_leg.value != 0:
            raise ValueError("native-value DEX calls are disabled for atomic ERC20 flash execution")

        params = (
            self.config.owner_address,
            int(Decimal(str(opportunity.min_profit_quote_units))),
            int(max_block_number),
            self._call(opportunity.first_leg),
            self._call(opportunity.second_leg),
        )
        fn = self.contract.functions.executeFlashArbitrage(
            Web3.to_checksum_address(opportunity.quote_token),
            int(opportunity.quote_amount),
            params,
        )
        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
        latest = self.w3.eth.get_block("latest")
        base_fee = int(latest.get("baseFeePerGas") or 0)
        priority = int(self.w3.to_wei(self.config.max_priority_fee_gwei, "gwei"))
        max_fee = int(Decimal(max(base_fee + priority, priority)) * self.config.max_fee_multiplier)
        tx: dict[str, Any] = fn.build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "chainId": self.config.chain_id,
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority,
            "value": 0,
        })
        if self.config.gas_limit is None:
            tx["gas"] = int(self.w3.eth.estimate_gas(tx) * 1.10)
        else:
            tx["gas"] = self.config.gas_limit

        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        return tx_hash.hex()
