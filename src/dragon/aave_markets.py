from __future__ import annotations

"""Aave V3 market discovery for Dragon.

This is the backend equivalent of the Aave React useAaveMarkets hook:
given chain IDs, Dragon resolves the correct Aave V3 Pool and reads the
currently listed reserves directly from each chain.

The registry intentionally keeps only deployment metadata. Reserve state is
always read from the live Pool so stale token lists are not embedded in code.
"""

from dataclasses import dataclass
from typing import Any

from web3 import Web3


POOL_ABI = [
    {
        "inputs": [],
        "name": "getReservesList",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function",
    },
]


@dataclass(frozen=True)
class AaveV3Deployment:
    chain_id: int
    name: str
    pool: str


# Official Aave V3 deployments used by Dragon's initial market-discovery set.
# Ethereum Core and Base have different pools; do not reuse the Base address
# as the Ethereum default.
AAVE_V3_DEPLOYMENTS: dict[int, AaveV3Deployment] = {
    1: AaveV3Deployment(
        chain_id=1,
        name="Ethereum Core",
        pool="0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",
    ),
    8453: AaveV3Deployment(
        chain_id=8453,
        name="Base",
        pool="0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",
    ),
}


def get_aave_v3_deployment(chain_id: int) -> AaveV3Deployment:
    try:
        return AAVE_V3_DEPLOYMENTS[int(chain_id)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"unsupported Aave V3 market chain: {chain_id}") from exc


@dataclass(frozen=True)
class AaveMarket:
    chain_id: int
    name: str
    pool: str
    reserves: tuple[str, ...]


@dataclass(frozen=True)
class AaveReserve:
    chain_id: int
    market: str
    token: str


class AaveMarketDiscovery:
    """Discover live Aave V3 reserves for one or more configured chains."""

    def __init__(self, rpc_urls: dict[int, str]):
        self.rpc_urls = {int(chain): url.strip() for chain, url in rpc_urls.items() if url.strip()}

    def discover(self, chain_ids: list[int] | tuple[int, ...]) -> list[AaveReserve]:
        reserves: list[AaveReserve] = []
        for raw_chain_id in chain_ids:
            chain_id = int(raw_chain_id)
            deployment = get_aave_v3_deployment(chain_id)
            rpc_url = self.rpc_urls.get(chain_id)
            if not rpc_url:
                raise RuntimeError(f"missing RPC URL for Aave chain {chain_id}")
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            if not w3.is_connected():
                raise RuntimeError(f"cannot connect to RPC for Aave chain {chain_id}")
            if int(w3.eth.chain_id) != chain_id:
                raise RuntimeError(
                    f"Aave RPC chain mismatch: expected {chain_id}, got {w3.eth.chain_id}"
                )
            pool = w3.eth.contract(
                address=Web3.to_checksum_address(deployment.pool),
                abi=POOL_ABI,
            )
            tokens = pool.functions.getReservesList().call()
            reserves.extend(
                AaveReserve(
                    chain_id=chain_id,
                    market=deployment.name,
                    token=Web3.to_checksum_address(token),
                )
                for token in tokens
            )
        return reserves

    def discover_by_chain(self, chain_ids: list[int] | tuple[int, ...]) -> dict[int, list[AaveReserve]]:
        result: dict[int, list[AaveReserve]] = {}
        for reserve in self.discover(chain_ids):
            result.setdefault(reserve.chain_id, []).append(reserve)
        return result


    def discover_markets(self, chain_ids: list[int] | tuple[int, ...]) -> list[AaveMarket]:
        """Return live Aave V3 market snapshots for the requested chains."""
        markets: list[AaveMarket] = []
        for raw_chain_id in chain_ids:
            chain_id = int(raw_chain_id)
            deployment = get_aave_v3_deployment(chain_id)
            rpc_url = self.rpc_urls.get(chain_id)
            if not rpc_url:
                raise RuntimeError(f"missing RPC URL for Aave chain {chain_id}")
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 10}))
            if not w3.is_connected():
                raise RuntimeError(f"cannot connect to RPC for Aave chain {chain_id}")
            if int(w3.eth.chain_id) != chain_id:
                raise RuntimeError(f"Aave RPC chain mismatch: expected {chain_id}, got {w3.eth.chain_id}")
            pool = w3.eth.contract(address=Web3.to_checksum_address(deployment.pool), abi=POOL_ABI)
            reserves = tuple(Web3.to_checksum_address(token) for token in pool.functions.getReservesList().call())
            if not reserves:
                raise RuntimeError(f"Aave market {chain_id} returned no reserves")
            markets.append(AaveMarket(chain_id, deployment.name, Web3.to_checksum_address(deployment.pool), reserves))
        return markets
