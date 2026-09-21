from __future__ import annotations

"""Live Base chain connector for Dragon.

This module performs read-only on-chain reads. It deliberately does not sign,
send, or approve transactions.
"""

import os
import time
from decimal import Decimal
from web3 import Web3


BASE_CHAIN_ID = 8453
BASE_RPC_DEFAULT = "https://base-rpc.publicnode.com"
AAVE_V3_BASE_POOL = Web3.to_checksum_address("0xA238Dd80C259a72e81d7e4664a9801593F98d1c5")
AAVE_V3_BASE_PROVIDER = Web3.to_checksum_address("0xe20fCBdBfFC4Dd138cE8b2E6FBb6CB49777ad64D")

POOL_ABI = [
    {"inputs":[],"name":"ADDRESSES_PROVIDER","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
    {"inputs":[],"name":"getReservesList","outputs":[{"type":"address[]"}],"stateMutability":"view","type":"function"},
    {"inputs":[{"name":"asset","type":"address"}],"name":"getReserveData","outputs":[
        {"name":"configuration","type":"uint256"},
        {"name":"liquidityIndex","type":"uint128"},
        {"name":"currentLiquidityRate","type":"uint128"},
        {"name":"variableBorrowIndex","type":"uint128"},
        {"name":"currentVariableBorrowRate","type":"uint128"},
        {"name":"currentStableBorrowRate","type":"uint128"},
        {"name":"lastUpdateTimestamp","type":"uint40"},
        {"name":"aTokenAddress","type":"address"},
        {"name":"stableDebtTokenAddress","type":"address"},
        {"name":"variableDebtTokenAddress","type":"address"},
        {"name":"interestRateStrategyAddress","type":"address"},
        {"name":"accruedToTreasury","type":"uint128"},
        {"name":"unbacked","type":"uint128"},
        {"name":"isolationModeTotalDebt","type":"uint128"}
    ],"stateMutability":"view","type":"function"}
]

class BaseLiveReader:
    def __init__(self, rpc_url: str | None = None, timeout: float = 8.0):
        self.rpc_url = rpc_url or os.getenv("BASE_RPC_URL") or os.getenv("DEX_RPC_URL") or BASE_RPC_DEFAULT
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": timeout}))
        self.pool = self.w3.eth.contract(address=AAVE_V3_BASE_POOL, abi=POOL_ABI)

    def snapshot(self) -> dict:
        started = time.perf_counter()
        chain_id = int(self.w3.eth.chain_id)
        block = self.w3.eth.get_block("latest")
        if chain_id != BASE_CHAIN_ID:
            raise RuntimeError(f"wrong Base chain id: expected {BASE_CHAIN_ID}, got {chain_id}")

        reserves = self.pool.functions.getReservesList().call()
        reserve_rows = []
        for asset in reserves:
            try:
                data = self.pool.functions.getReserveData(asset).call()
                reserve_rows.append({
                    "asset": asset,
                    "current_liquidity_rate_ray": str(data[2]),
                    "current_variable_borrow_rate_ray": str(data[4]),
                    "last_update_timestamp": int(data[6]),
                    "a_token": data[7],
                    "variable_debt_token": data[9],
                    "interest_rate_strategy": data[10],
                    "unbacked": str(data[12]),
                    "isolation_mode_total_debt": str(data[13]),
                })
            except Exception as exc:
                reserve_rows.append({"asset": asset, "error": f"{type(exc).__name__}: {exc}"})

        return {
            "connected": True,
            "chain_id": chain_id,
            "block_number": int(block["number"]),
            "block_timestamp": int(block["timestamp"]),
            "base_fee_wei": str(block.get("baseFeePerGas", 0)),
            "rpc_latency_ms": round((time.perf_counter() - started) * 1000, 2),
            "aave_v3": {
                "pool": AAVE_V3_BASE_POOL,
                "provider": AAVE_V3_BASE_PROVIDER,
                "reserve_count": len(reserves),
                "reserves": reserve_rows,
            },
            "v4_status": "not_live_on_base_verified_by_current_aave_deployment_status",
        }
