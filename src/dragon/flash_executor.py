from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3
from web3.contract import Contract


_CALL_COMPONENTS = [
    {"internalType": "address", "name": "target", "type": "address"},
    {"internalType": "bytes", "name": "data", "type": "bytes"},
    {"internalType": "address", "name": "sellToken", "type": "address"},
    {"internalType": "address", "name": "buyToken", "type": "address"},
    {"internalType": "address", "name": "allowanceTarget", "type": "address"},
    {"internalType": "uint256", "name": "sellAmount", "type": "uint256"},
    {"internalType": "uint256", "name": "minBuyAmount", "type": "uint256"},
]
_FLASH_PARAMS_COMPONENTS = [
    {"internalType": "address", "name": "owner", "type": "address"},
    {"internalType": "uint256", "name": "minProfit", "type": "uint256"},
    {"internalType": "uint256", "name": "maxBlockNumber", "type": "uint256"},
    {"internalType": "uint256", "name": "compoundAmount", "type": "uint256"},
    {"internalType": "bool", "name": "compoundProfit", "type": "bool"},
    {"components": _CALL_COMPONENTS, "internalType": "struct DragonAaveV3Executor.Call", "name": "first", "type": "tuple"},
    {"components": _CALL_COMPONENTS, "internalType": "struct DragonAaveV3Executor.Call", "name": "second", "type": "tuple"},
]
EXECUTOR_ABI = [
    {"inputs": [
        {"internalType": "address", "name": "asset", "type": "address"},
        {"internalType": "uint256", "name": "amount", "type": "uint256"},
        {"components": _FLASH_PARAMS_COMPONENTS, "internalType": "struct DragonAaveV3Executor.FlashParams", "name": "params", "type": "tuple"},
    ], "name": "executeFlashArbitrage", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"anonymous": False, "inputs": [
        {"indexed": True, "internalType": "address", "name": "asset", "type": "address"},
        {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "premium", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "profit", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "compoundAmount", "type": "uint256"},
        {"indexed": False, "internalType": "uint256", "name": "retainedProfit", "type": "uint256"},
        {"indexed": True, "internalType": "address", "name": "firstTarget", "type": "address"},
        {"indexed": True, "internalType": "address", "name": "secondTarget", "type": "address"},
    ], "name": "FlashArbitrageExecuted", "type": "event"},
]
ERC20_ABI = [{"constant":True,"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"constant":True,"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
AAVE_POOL_ABI = [{"inputs":[],"name":"FLASHLOAN_PREMIUM_TOTAL","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"}]


class FlashTransactionReverted(RuntimeError):
    def __init__(self, tx_hash: str, receipt: Any):
        super().__init__(f"flash transaction reverted: {tx_hash}")
        self.tx_hash = tx_hash
        self.receipt = dict(receipt)


@dataclass(frozen=True)
class FlashExecutorConfig:
    rpc_url: str
    private_key: str
    executor_address: str
    owner_address: str
    pool_address: str
    chain_id: int
    quote_token_decimals: int = 6
    min_profit_quote: Decimal = Decimal("0.005")
    gas_limit: int | None = None
    max_fee_multiplier: Decimal = Decimal("1.20")
    max_priority_fee_gwei: Decimal = Decimal("0.001")
    mev_required: bool = True
    compound_profits: bool = False
    wrapped_native_token: str = ""

    @classmethod
    def from_env(cls) -> "FlashExecutorConfig":
        rpc = os.getenv("DEX_PRIVATE_RPC_URL", "").strip()
        key = os.getenv("DEX_EXECUTOR_OWNER_PRIVATE_KEY", "").strip()
        executor = os.getenv("DEX_EXECUTOR_ADDRESS", "").strip()
        owner = os.getenv("DEX_EXECUTOR_OWNER_ADDRESS", "").strip()
        if not rpc or not key or not executor or not owner:
            raise RuntimeError("live flash execution requires DEX_PRIVATE_RPC_URL, DEX_EXECUTOR_OWNER_PRIVATE_KEY, DEX_EXECUTOR_OWNER_ADDRESS and DEX_EXECUTOR_ADDRESS")
        chain_id = int(os.getenv("DEX_EXECUTOR_CHAIN_ID", os.getenv("DEX_CHAIN_ID", "8453")))
        # Resolve the default Aave V3 Pool from the selected chain. An explicit
        # environment override remains available for custom deployments.
        from .aave_markets import get_aave_v3_deployment
        pool = os.getenv("DEX_AAVE_POOL_ADDRESS", "").strip() or get_aave_v3_deployment(chain_id).pool
        decimals = int(os.getenv("DEX_QUOTE_TOKEN_DECIMALS", "6"))
        min_profit = Decimal(os.getenv("DEX_MIN_NET_PROFIT", "0.005"))
        multiplier = Decimal(os.getenv("DEX_MAX_FEE_MULTIPLIER", "1.20"))
        if chain_id <= 0 or not 0 <= decimals <= 36 or min_profit < Decimal("0.005") or multiplier < Decimal("1"):
            raise ValueError("invalid flash executor chain/decimals/min-profit/fee configuration")
        return cls(
            rpc_url=rpc, private_key=key, executor_address=Web3.to_checksum_address(executor),
            owner_address=Web3.to_checksum_address(owner), pool_address=Web3.to_checksum_address(pool),
            chain_id=chain_id, quote_token_decimals=decimals, min_profit_quote=min_profit,
            gas_limit=int(os.getenv("DEX_EXECUTOR_GAS_LIMIT", "0")) or None,
            max_fee_multiplier=multiplier, max_priority_fee_gwei=Decimal(os.getenv("DEX_MAX_PRIORITY_FEE_GWEI", "0.001")),
            mev_required=os.getenv("MEV_PROTECTION_REQUIRED", "true").lower() in {"1","true","yes","on"},
            compound_profits=False,
            wrapped_native_token=Web3.to_checksum_address(os.getenv("DEX_WRAPPED_NATIVE_TOKEN", "0x0000000000000000000000000000000000000000")) if os.getenv("DEX_WRAPPED_NATIVE_TOKEN", "").strip() else "",
        )


class AaveFlashExecutor:
    """Build/sign/send one atomic flash-loan cycle through a private RPC."""

    def __init__(self, config: FlashExecutorConfig | None = None):
        self.config = config or FlashExecutorConfig.from_env()
        if self.config.mev_required and not self.config.rpc_url:
            raise RuntimeError("MEV protection requires a private RPC endpoint")
        self.w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout":10}))
        if not self.w3.is_connected():
            raise RuntimeError("cannot connect to configured private RPC")
        if self.w3.eth.chain_id != self.config.chain_id:
            raise RuntimeError("configured chain ID does not match private RPC")
        self.contract: Contract = self.w3.eth.contract(address=self.config.executor_address, abi=EXECUTOR_ABI)
        self.pool = self.w3.eth.contract(address=self.config.pool_address, abi=AAVE_POOL_ABI)
        self.account = self.w3.eth.account.from_key(self.config.private_key)
        if self.account.address != self.config.owner_address:
            raise RuntimeError("executor private key does not match DEX_EXECUTOR_OWNER_ADDRESS")

    def available_liquidity_units(self, token: str) -> int:
        token_contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return int(token_contract.functions.balanceOf(self.config.pool_address).call())

    def compound_balance_units(self, token: str) -> int:
        token_contract = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
        return int(token_contract.functions.balanceOf(self.config.executor_address).call())

    def flash_loan_fee_bps(self) -> Decimal:
        """Read Aave V3 FLASHLOAN_PREMIUM_TOTAL as basis points.

        Aave exposes the premium in basis points (e.g. 5 = 5 bps = 0.05%),
        so the raw on-chain value must NOT be divided by 10,000 here.
        """
        raw_bps = Decimal(str(self.pool.functions.FLASHLOAN_PREMIUM_TOTAL().call()))
        if raw_bps < 0 or raw_bps > Decimal("1000"):
            raise RuntimeError(f"invalid Aave flash-loan premium: {raw_bps} bps")
        return raw_bps

    @staticmethod
    def _call(execution: Any) -> tuple[Any, ...]:
        if not execution.allowance_target:
            raise ValueError("DEX execution is missing allowance target")
        return (
            Web3.to_checksum_address(execution.to), execution.data,
            Web3.to_checksum_address(execution.sell_token), Web3.to_checksum_address(execution.buy_token),
            Web3.to_checksum_address(execution.allowance_target), int(execution.sell_amount), int(execution.buy_amount),
        )

    def build_and_send(self, opportunity: Any, *, max_block_number: int) -> str:
        total_amount = int(opportunity.quote_amount)
        compound_amount = int(getattr(opportunity, "compound_amount", 0))
        flash_amount = total_amount - compound_amount
        if total_amount <= 0 or flash_amount <= 0:
            raise ValueError("flash amount must be positive")
        if compound_amount != 0 or self.config.compound_profits:
            raise ValueError("Dragon is configured for no compounding; compound amount must be zero")
        if opportunity.net_profit_quote < self.config.min_profit_quote:
            raise ValueError("opportunity is below minimum net profit")
        if max_block_number < self.w3.eth.block_number:
            raise ValueError("max_block_number is already expired")
        if opportunity.first_leg.value != 0 or opportunity.second_leg.value != 0:
            raise ValueError("native-value DEX calls are disabled for atomic ERC20 flash execution")
        if opportunity.first_leg.sell_amount != total_amount:
            raise ValueError("first leg sell amount does not match total flash-plus-compound amount")
        if opportunity.first_leg.buy_amount < opportunity.second_leg.sell_amount:
            raise ValueError("second leg attempts to sell more base than the first leg produces")
        if opportunity.second_leg.buy_amount < total_amount:
            raise ValueError("second leg quote output is below the total principal")

        scale = Decimal(10) ** self.config.quote_token_decimals
        # Gas is paid by the transaction sender in the native asset and is already
        # deducted once by the scanner when it computes opportunity.net_profit_quote.
        # Do not add estimated gas a second time to the token-denominated contract
        # profit gate. The contract only needs to enforce gross quote profit after
        # the Aave premium; the off-chain gate enforces true net profit after gas.
        required_profit = self.config.min_profit_quote
        if required_profit <= 0 or not required_profit.is_finite():
            raise ValueError("invalid required net-profit threshold")

        params = (
            self.config.owner_address, int(required_profit * scale), int(max_block_number),
            compound_amount, self.config.compound_profits,
            self._call(opportunity.first_leg), self._call(opportunity.second_leg),
        )
        fn = self.contract.functions.executeFlashArbitrage(
            Web3.to_checksum_address(opportunity.quote_token), flash_amount, params
        )
        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
        latest = self.w3.eth.get_block("latest")
        base_fee = int(latest.get("baseFeePerGas") or 0)
        priority = int(self.w3.to_wei(self.config.max_priority_fee_gwei, "gwei"))
        max_fee = int(Decimal(max(base_fee + priority, priority)) * self.config.max_fee_multiplier)
        tx: dict[str, Any] = {"from":self.account.address,"nonce":nonce,"chainId":self.config.chain_id,"maxFeePerGas":max_fee,"maxPriorityFeePerGas":priority,"value":0}
        tx.update(fn.build_transaction(tx))
        tx["gas"] = self.config.gas_limit or int(self.w3.eth.estimate_gas(tx) * Decimal(os.getenv("DEX_GAS_ESTIMATE_MULTIPLIER", "1.05")))
        # estimate_gas is not enough for lifecycle observability. Run the exact
        # calldata through eth_call immediately before signing so a later record
        # can distinguish simulation failure from submission failure.
        self.w3.eth.call({
            "from": self.account.address,
            "to": self.config.executor_address,
            "data": tx["data"],
            "value": 0,
            "gas": tx["gas"],
            "maxFeePerGas": tx["maxFeePerGas"],
            "maxPriorityFeePerGas": tx["maxPriorityFeePerGas"],
        })
        signed = self.account.sign_transaction(tx)
        return self.w3.eth.send_raw_transaction(signed.raw_transaction).hex()

    def wait_for_success(
        self,
        tx_hash: str,
        timeout: int = 30,
        *,
        native_to_quote_rate: Decimal | None = None,
        opportunity: Any | None = None,
    ) -> dict[str, Any]:
        """Wait for inclusion and calculate realized P&L without double counting.

        The executor contract's profit event field is already the quote-token
        profit after the Aave premium and before transaction gas. Therefore:
        realized_net = event profit - actual receipt gas converted to quote.

        native_to_quote_rate is the execution-time native-token/quote-token
        conversion supplied by the caller. If unavailable, raw gas cost and
        token profit are still returned, but realized net P&L is not reported.
        """
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        gas_used = int(receipt.get("gasUsed", 0) or 0)
        gas_price = int(receipt.get("effectiveGasPrice", receipt.get("gasPrice", 0)) or 0)
        gas_cost_native = gas_used * gas_price
        if int(receipt.get("status", 0)) != 1:
            reverted = dict(receipt)
            reverted["gas_cost_native"] = str(gas_cost_native)
            if native_to_quote_rate is not None:
                rate = Decimal(native_to_quote_rate)
                if rate.is_finite() and rate > 0:
                    reverted["gas_cost_quote"] = str(
                        (Decimal(gas_cost_native) / (Decimal(10) ** 18)) * rate
                    )
            raise FlashTransactionReverted(tx_hash, reverted)

        result = dict(receipt)
        result["gas_cost_native"] = str(gas_cost_native)

        # Gas conversion must come from a fresh execution-time price supplied
        # by the caller. Do not infer it from token-unit amounts: native and
        # quote assets can have different decimals, and the first-leg output
        # is not necessarily the gas asset.
        try:
            events = self.contract.events.FlashArbitrageExecuted().process_receipt(receipt)
            if events:
                args = events[-1]["args"]
                profit_raw = int(args.get("profit", 0))
                premium_raw = int(args.get("premium", 0))
                result["arb_profit_raw"] = str(profit_raw)
                result["arb_premium_raw"] = str(premium_raw)
                result["compound_amount_raw"] = str(args.get("compoundAmount", 0))
                result["retained_profit_raw"] = str(args.get("retainedProfit", 0))

                scale = Decimal(10) ** self.config.quote_token_decimals
                result["arb_profit_quote"] = str(Decimal(profit_raw) / scale)
                result["arb_premium_quote"] = str(Decimal(premium_raw) / scale)

                # Event profit already subtracts the Aave premium. Never
                # subtract arb_premium_quote again when computing realized P&L.
                if native_to_quote_rate is not None:
                    rate = Decimal(native_to_quote_rate)
                    if not rate.is_finite() or rate <= 0:
                        raise ValueError("native_to_quote_rate must be finite and positive")
                    gas_quote = (Decimal(gas_cost_native) / (Decimal(10) ** 18)) * rate
                    realized = (Decimal(profit_raw) / scale) - gas_quote
                    result["gas_cost_quote"] = str(gas_quote)
                    result["realized_pnl_quote"] = str(realized)
        except Exception:
            # A successful receipt without a decodable executor event is still
            # included, but realized token proceeds remain unknown.
            pass
        return result
