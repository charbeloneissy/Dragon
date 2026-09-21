from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LaunchChain:
    chain_id: int
    name: str
    venue: str


LAUNCH_CHAINS = (
    LaunchChain(8453, "base", "aerodrome"),
    LaunchChain(56, "bnb", "dex"),
    LaunchChain(42220, "celo", "dex"),
)


def is_launch_chain(chain_id: int) -> bool:
    return int(chain_id) in {c.chain_id for c in LAUNCH_CHAINS}
