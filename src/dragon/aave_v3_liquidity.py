from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3

from .aave_markets import get_aave_v3_deployment


ERC20_BALANCE_ABI = [{
    "constant": True,
    "inputs": [{"name": "account", "type": "address"}],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}]

POOL_FEE_ABI = [{
    "inputs": [],
    "name": "FLASHLOAN_PREMIUM_TOTAL",
    "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}],
    "stateMutability": "view",
    "type": "function",
}]


@dataclass(frozen=True)
class AaveV3LiquiditySnapshot:
    chain_id: int
    pool: str
    asset: str
    available_units: int
    premium_bps: Decimal
    block_number: int

    @property
    def available(self) -> Decimal:
        return Decimal(self.available_units)


class AaveV3LiquidityProvider:
    """Live Aave V3 liquidity/fee adapter used by Dragon's optimizer.

    This is intentionally read-only. The atomic executor remains the only
    component allowed to initiate a flash loan.
    """

    def __init__(self, rpc_urls: dict[int, str], *, timeout: int = 5):
        self.rpc_urls = {int(k): str(v) for k, v in rpc_urls.items()}
        self.timeout = int(timeout)

    def snapshot(self, chain_id: int, asset: str) -> AaveV3LiquiditySnapshot:
        chain_id = int(chain_id)
        deployment = get_aave_v3_deployment(chain_id)
        rpc = self.rpc_urls.get(chain_id)
        if not rpc:
            raise RuntimeError(f"missing RPC URL for Aave V3 chain {chain_id}")

        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": self.timeout}))
        if not w3.is_connected():
            raise RuntimeError(f"cannot connect to Aave V3 RPC for chain {chain_id}")
        actual_chain = int(w3.eth.chain_id)
        if actual_chain != chain_id:
            raise RuntimeError(
                f"Aave V3 RPC chain mismatch: expected {chain_id}, got {actual_chain}"
            )

        pool = w3.eth.contract(
            address=Web3.to_checksum_address(deployment.pool),
            abi=POOL_FEE_ABI,
        )
        token = w3.eth.contract(
            address=Web3.to_checksum_address(asset),
            abi=ERC20_BALANCE_ABI,
        )
        return AaveV3LiquiditySnapshot(
            chain_id=chain_id,
            pool=Web3.to_checksum_address(deployment.pool),
            asset=Web3.to_checksum_address(asset),
            available_units=int(token.functions.balanceOf(deployment.pool).call()),
            premium_bps=Decimal(str(pool.functions.FLASHLOAN_PREMIUM_TOTAL().call())),
            block_number=int(w3.eth.block_number),
        )

    def max_flash_amount(self, chain_id: int, asset: str, requested_units: int) -> int:
        if requested_units <= 0:
            return 0
        snap = self.snapshot(chain_id, asset)
        return min(int(requested_units), snap.available_units)
