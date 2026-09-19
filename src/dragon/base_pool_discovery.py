from __future__ import annotations

import logging
from typing import Iterable

from web3 import Web3

BASE_WETH = "0x4200000000000000000000000000000000000006"
CURATED_PAPER_BASE_TOKENS = (
    BASE_WETH,
    "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",  # cbBTC
    "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",  # cbETH
    "0x940181a94A35A4569E4529A3CDfB74e38FD98631",  # AERO
    "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",  # DAI
)
UNISWAP_V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
AERODROME_DEFAULT_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"

UNI_POOL_CREATED = Web3.keccak(text="PoolCreated(address,address,uint24,int24,address)").hex()
AERO_POOL_CREATED = Web3.keccak(text="PoolCreated(address,address,bool,address,uint256)").hex()


def _topic_address(value: str) -> str:
    return Web3.to_checksum_address("0x" + value[-40:])


def discover_recent_base_tokens(
    adapter,
    *,
    quote_token: str,
    anchors: Iterable[str] = (),
    lookback_blocks: int = 250000,
    chunk_blocks: int = 2000,
    max_tokens: int = 250,
) -> list[str]:
    """Discover recent tokens from Base pool-creation events.

    Only pools involving the configured quote token or bridge anchors are
    considered. The existing two-DEX quote engine remains the final liquidity
    and profitability gate.
    """
    if not getattr(adapter, "w3", None):
        return []

    lookback_blocks = max(100, min(int(lookback_blocks), 250000))
    chunk_blocks = max(100, min(int(chunk_blocks), 10000))
    max_tokens = max(1, min(int(max_tokens), 250))

    anchors_set = {Web3.to_checksum_address(quote_token).lower()}
    for value in anchors:
        try:
            anchors_set.add(Web3.to_checksum_address(value).lower())
        except Exception:
            continue

    latest = int(adapter._rpc_call(lambda w3: w3.eth.block_number))
    start = max(0, latest - lookback_blocks)
    # Track both recency and how many independent pools expose the token.
    # Pool count is a conservative liquidity/availability proxy during discovery;
    # executable quote quality remains the final liquidity gate.
    discovered: dict[str, dict[str, int]] = {}

    # The direct quote adapter currently supports Aerodrome's v2 factory.
    # Slipstream factories are intentionally excluded until their CL quote path
    # is implemented in the execution adapter.
    factories = (
        (UNISWAP_V3_FACTORY, UNI_POOL_CREATED),
        (AERODROME_DEFAULT_FACTORY, AERO_POOL_CREATED),
    )

    for factory, event_topic in factories:
        for chunk_start in range(start, latest + 1, chunk_blocks):
            chunk_end = min(latest, chunk_start + chunk_blocks - 1)
            try:
                logs = adapter._rpc_call(
                    lambda w3, f=factory, a=chunk_start, b=chunk_end,
                    topic=event_topic: w3.eth.get_logs({
                        "fromBlock": a,
                        "toBlock": b,
                        "address": Web3.to_checksum_address(f),
                        "topics": [topic],
                    })
                )
            except Exception as exc:
                logging.debug(
                    "universe discovery query failed factory=%s blocks=%s-%s: %s",
                    factory, chunk_start, chunk_end, exc,
                )
                continue

            for log in logs:
                topics = log.get("topics") or []
                if len(topics) < 3 or str(topics[0]).lower() != event_topic.lower():
                    continue
                try:
                    token0 = _topic_address(str(topics[1]))
                    token1 = _topic_address(str(topics[2]))
                    block = int(log["blockNumber"])
                except (KeyError, TypeError, ValueError):
                    continue

                token0_l = token0.lower()
                token1_l = token1.lower()
                if token0_l not in anchors_set and token1_l not in anchors_set:
                    continue

                for token in (token0, token1):
                    if token.lower() not in anchors_set:
                        key = token.lower()
                        entry = discovered.setdefault(key, {"latest_block": 0, "pool_count": 0})
                        entry["latest_block"] = max(block, entry["latest_block"])
                        entry["pool_count"] += 1

    ranked = sorted(
        discovered.items(),
        key=lambda item: (item[1]["pool_count"], item[1]["latest_block"]),
        reverse=True,
    )
    result = [Web3.to_checksum_address(token) for token, _ in ranked[:max_tokens]]
    logging.info(
        "DEX universe discovery complete latest_block=%s lookback=%s candidates=%s top_pool_count=%s",
        latest, lookback_blocks, len(result),
        ranked[0][1]["pool_count"] if ranked else 0,
    )
    return result
