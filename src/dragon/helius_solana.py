"""Helius-backed Solana infrastructure for Dragon.

This adapter is deliberately transport-focused: it provides authenticated Helius
RPC access, dynamic priority-fee estimation, and Helius transaction/transfer
queries without embedding strategy decisions.

LIVE_TRADING remains controlled by Dragon's existing execution gates.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import httpx


DEFAULT_HELIUS_RPC = "https://mainnet.helius-rpc.com/"
DEFAULT_HELIUS_API = "https://api.helius.xyz"


class HeliusError(RuntimeError):
    """Raised when Helius returns an API or RPC error."""


@dataclass(frozen=True)
class HeliusConfig:
    api_key: str
    rpc_url: str = DEFAULT_HELIUS_RPC
    api_url: str = DEFAULT_HELIUS_API
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> "HeliusConfig":
        return cls(
            api_key=os.getenv("HELIUS_API_KEY", "").strip(),
            rpc_url=os.getenv("HELIUS_RPC_URL", DEFAULT_HELIUS_RPC).strip()
            or DEFAULT_HELIUS_RPC,
            api_url=os.getenv("HELIUS_API_URL", DEFAULT_HELIUS_API).strip()
            or DEFAULT_HELIUS_API,
            timeout_seconds=float(os.getenv("HELIUS_TIMEOUT_SECONDS", "8")),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


class HeliusSolanaClient:
    """Small async Helius client used by Dragon's Solana world model."""

    def __init__(self, config: HeliusConfig | None = None) -> None:
        self.config = config or HeliusConfig.from_env()

    def _require_key(self) -> None:
        if not self.config.api_key:
            raise HeliusError("HELIUS_API_KEY is not configured")

    def _rpc_url(self) -> str:
        separator = "&" if "?" in self.config.rpc_url else "?"
        return f"{self.config.rpc_url}{separator}api-key={self.config.api_key}"

    async def rpc(self, method: str, params: list[Any] | None = None) -> Any:
        self._require_key()
        payload = {
            "jsonrpc": "2.0",
            "id": "dragon-helius",
            "method": method,
            "params": params or [],
        }
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(self._rpc_url(), json=payload)
            response.raise_for_status()
            data = response.json()

        if "error" in data:
            error = data["error"]
            raise HeliusError(
                f"Helius RPC {method} failed: "
                f"{error.get('code', 'unknown')} {error.get('message', error)}"
            )
        return data.get("result")

    async def get_health(self) -> Any:
        return await self.rpc("getHealth")

    async def get_slot(self) -> int:
        return int(await self.rpc("getSlot"))

    async def get_balance(self, address: str) -> int:
        result = await self.rpc("getBalance", [address])
        return int(result["value"])

    async def get_token_accounts_by_owner(
        self, owner: str, mint: str | None = None
    ) -> Any:
        if mint:
            return await self.rpc(
                "getTokenAccountsByOwner",
                [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
            )
        return await self.rpc(
            "getTokenAccountsByOwner",
            [owner, {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
             {"encoding": "jsonParsed"}],
        )

    async def get_priority_fee_estimate(
        self,
        *,
        account_keys: list[str] | None = None,
        transaction: str | None = None,
        priority_level: str | None = None,
        recommended: bool = True,
        include_all_levels: bool = False,
        lookback_slots: int | None = None,
    ) -> dict[str, Any]:
        """Return Helius dynamic priority-fee data.

        Callers should provide either account_keys or a base58 serialized
        transaction. Helius calculates the estimate against the relevant local
        fee market.
        """
        if not account_keys and not transaction:
            raise ValueError("account_keys or transaction is required")
        if account_keys and transaction:
            raise ValueError("provide account_keys or transaction, not both")

        target: dict[str, Any] = {}
        if account_keys:
            target["accountKeys"] = account_keys
        else:
            target["transaction"] = transaction

        options: dict[str, Any] = {
            "recommended": recommended,
            "includeAllPriorityFeeLevels": include_all_levels,
        }
        if priority_level:
            options["priorityLevel"] = priority_level
        if lookback_slots is not None:
            if lookback_slots <= 0:
                raise ValueError("lookback_slots must be positive")
            options["lookbackSlots"] = lookback_slots

        result = await self.rpc("getPriorityFeeEstimate", [{**target, "options": options}])
        return result or {}

    async def get_transactions_for_address(
        self,
        address: str,
        *,
        limit: int = 100,
        pagination_token: str | None = None,
        transaction_details: str = "full",
    ) -> Any:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: dict[str, Any] = {
            "address": address,
            "options": {
                "limit": limit,
                "transactionDetails": transaction_details,
            },
        }
        if pagination_token:
            params["paginationToken"] = pagination_token
        return await self.rpc("getTransactionsForAddress", [params])

    async def get_transfers_by_address(
        self,
        address: str,
        *,
        limit: int = 100,
        mint: str | None = None,
    ) -> Any:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        params: dict[str, Any] = {"address": address, "limit": limit}
        if mint:
            params["mint"] = mint
        return await self.rpc("getTransfersByAddress", [params])

    async def get_enhanced_transactions(
        self,
        address: str,
        *,
        limit: int = 20,
        before_signature: str | None = None,
    ) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self._require_key()
        url = (
            f"{self.config.api_url.rstrip('/')}/v0/addresses/"
            f"{address}/transactions"
        )
        params: dict[str, Any] = {"api-key": self.config.api_key, "limit": limit}
        if before_signature:
            params["before-signature"] = before_signature

        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, list):
            raise HeliusError("Helius Enhanced Transactions returned a non-list response")
        return data
