"""Network-aware Base adapter with strict mainnet execution isolation.

The adapter intentionally separates:
* read-only discovery/simulation on Base Mainnet and Base Sepolia;
* live execution, which is permitted only on Base Mainnet (8453).

No Sepolia venue addresses are inferred from mainnet.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Callable, Iterable, Any

from web3 import Web3

from .chains import ChainSpec, get_spec, rpc_urls_for
from .venues import Venue, venues_for


BASE_MAINNET = 8453
BASE_SEPOLIA = 84532
BASE_CHAIN_IDS = frozenset({BASE_MAINNET, BASE_SEPOLIA})


class BaseNetworkError(RuntimeError):
    """Raised when the adapter is pointed at an invalid Base network."""


class LiveExecutionBlocked(BaseNetworkError):
    """Raised when live execution is attempted outside Base Mainnet."""


@dataclass(frozen=True)
class BaseNetworkProfile:
    chain_id: int
    spec: ChainSpec
    live_execution_allowed: bool

    @property
    def is_mainnet(self) -> bool:
        return self.chain_id == BASE_MAINNET

    @property
    def is_testnet(self) -> bool:
        return self.chain_id == BASE_SEPOLIA


def profile_for(chain_id: int) -> BaseNetworkProfile:
    cid = int(chain_id)
    if cid not in BASE_CHAIN_IDS:
        raise BaseNetworkError(
            f"unsupported Base chain id: expected one of {sorted(BASE_CHAIN_IDS)}, got {cid}"
        )
    spec = get_spec(cid)
    if spec is None:
        raise BaseNetworkError(f"missing chain registry entry for Base chain {cid}")
    return BaseNetworkProfile(
        chain_id=cid,
        spec=spec,
        live_execution_allowed=(cid == BASE_MAINNET),
    )


class BaseExecutionGuard:
    """Defense-in-depth gate for transaction execution.

    The guard is deliberately independent of the quote/discovery layer. A
    caller must prove the RPC chain id, configured chain id, and transaction
    target all belong to the same network before live execution is permitted.
    """

    def __init__(self, profile: BaseNetworkProfile):
        self.profile = profile

    def assert_live_execution_allowed(self) -> None:
        if not self.profile.live_execution_allowed:
            raise LiveExecutionBlocked(
                f"live execution is disabled on Base chain {self.profile.chain_id}; "
                "only Base Mainnet (8453) may broadcast"
            )

    def assert_chain(self, actual_chain_id: int) -> None:
        actual = int(actual_chain_id)
        if actual != self.profile.chain_id:
            raise BaseNetworkError(
                f"RPC chain mismatch: configured={self.profile.chain_id}, actual={actual}"
            )

    def assert_venue(self, venue: Venue) -> None:
        # Venue addresses are keyed by chain in the registry. A venue absent
        # from the configured chain is therefore never executable.
        if venue not in venues_for(self.profile.chain_id, include_disabled=True):
            raise BaseNetworkError(
                f"venue {venue.name!r} is not registered for Base chain {self.profile.chain_id}"
            )

    def authorize_broadcast(self, *, actual_chain_id: int, venue: Venue) -> None:
        self.assert_live_execution_allowed()
        self.assert_chain(actual_chain_id)
        self.assert_venue(venue)


class BaseNetworkAdapter:
    """Read-only discovery/simulation adapter for Base Mainnet and Sepolia."""

    def __init__(
        self,
        chain_id: int,
        *,
        rpc_url: str | None = None,
        timeout: float = 8.0,
        w3: Web3 | None = None,
    ):
        self.profile = profile_for(chain_id)
        self.guard = BaseExecutionGuard(self.profile)
        self.rpc_url = rpc_url or self._resolve_rpc_url()
        self.w3 = w3 or Web3(
            Web3.HTTPProvider(
                self.rpc_url,
                request_kwargs={"timeout": timeout},
            )
        )
        self._validate_rpc_identity()

    @classmethod
    def mainnet(cls, **kwargs: Any) -> "BaseNetworkAdapter":
        return cls(BASE_MAINNET, **kwargs)

    @classmethod
    def sepolia(cls, **kwargs: Any) -> "BaseNetworkAdapter":
        return cls(BASE_SEPOLIA, **kwargs)

    @property
    def chain_id(self) -> int:
        return self.profile.chain_id

    @property
    def live_execution_allowed(self) -> bool:
        return self.profile.live_execution_allowed

    @property
    def venues(self) -> tuple[Venue, ...]:
        return venues_for(self.chain_id)

    def _resolve_rpc_url(self) -> str:
        urls = rpc_urls_for(self.chain_id)
        if not urls:
            raise BaseNetworkError(f"no RPC configured for Base chain {self.chain_id}")
        return urls[0]

    def _validate_rpc_identity(self) -> None:
        try:
            actual = int(self.w3.eth.chain_id)
        except Exception as exc:
            raise BaseNetworkError(
                f"unable to read chain id from Base RPC {self.rpc_url!r}: {exc}"
            ) from exc
        self.guard.assert_chain(actual)

    def _rpc_call(self, fn: Callable[[Web3], Any]) -> Any:
        return fn(self.w3)

    def latest_block(self) -> int:
        return int(self._rpc_call(lambda w3: w3.eth.block_number))

    def gas_price_wei(self) -> int:
        return int(self._rpc_call(lambda w3: w3.eth.gas_price))

    def discover_tokens(
        self,
        *,
        quote_token: str,
        anchors: Iterable[str] = (),
        lookback_blocks: int = 25_000,
        chunk_blocks: int = 2_000,
        max_tokens: int = 250,
    ) -> list[str]:
        """Run the existing Base pool discovery against this adapter's network."""
        from .base_pool_discovery import discover_recent_base_tokens

        return discover_recent_base_tokens(
            self,
            quote_token=quote_token,
            anchors=anchors,
            lookback_blocks=lookback_blocks,
            chunk_blocks=chunk_blocks,
            max_tokens=max_tokens,
        )

    def simulate_call(self, transaction: dict[str, Any]) -> Any:
        """eth_call only; never signs or broadcasts."""
        tx = dict(transaction)
        if "chainId" in tx and int(tx["chainId"]) != self.chain_id:
            raise BaseNetworkError(
                f"simulation chain mismatch: tx={tx['chainId']} adapter={self.chain_id}"
            )
        return self._rpc_call(lambda w3: w3.eth.call(tx))

    def authorize_live_transaction(
        self,
        *,
        venue: Venue,
        actual_chain_id: int | None = None,
    ) -> None:
        """Final pre-broadcast gate; callers still perform signing/broadcasting."""
        actual = self.chain_id if actual_chain_id is None else int(actual_chain_id)
        self.guard.authorize_broadcast(actual_chain_id=actual, venue=venue)

    def network_status(self) -> dict[str, Any]:
        return {
            "network": self.profile.spec.name,
            "chain_id": self.chain_id,
            "rpc_url": self.rpc_url,
            "explorer": self.profile.spec.explorer,
            "live_execution_allowed": self.live_execution_allowed,
            "venue_count": len(self.venues),
            "venues": [v.name for v in self.venues],
        }


def build_base_adapters(
    *,
    include_sepolia: bool = True,
) -> dict[int, BaseNetworkAdapter]:
    """Construct adapters only for networks whose RPCs are reachable."""
    ids = [BASE_MAINNET, BASE_SEPOLIA] if include_sepolia else [BASE_MAINNET]
    adapters: dict[int, BaseNetworkAdapter] = {}
    errors: list[str] = []
    for cid in ids:
        try:
            adapters[cid] = BaseNetworkAdapter(cid)
        except BaseNetworkError as exc:
            errors.append(str(exc))
    if not adapters and errors:
        raise BaseNetworkError("; ".join(errors))
    return adapters
