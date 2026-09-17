from __future__ import annotations

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .dex import DexQuote


@dataclass(frozen=True)
class DexExecution:
    chain_id: int
    venue: str
    to: str
    data: str
    value: int
    gas: int | None
    gas_price: int | None
    sell_token: str
    buy_token: str
    sell_amount: int
    buy_amount: int
    allowance_target: str | None
    issues: dict[str, Any]


class ZeroXAdapter:
    """Real 0x Swap API v2 adapter for EVM DEX liquidity.

    It fetches firm executable quotes and exposes the returned transaction
    payload. It deliberately does not sign or broadcast transactions here;
    execution belongs behind Dragon's live-trading gate.
    """

    BASE_URL = "https://api.0x.org/swap/allowance-holder"

    def __init__(self, api_key: str | None = None, timeout: float = 4.0):
        self.api_key = (api_key or os.getenv("ZEROX_API_KEY", "")).strip()
        self.timeout = timeout
        self.client = httpx.Client(
            timeout=timeout,
            headers={
                "Accept": "application/json",
                "0x-version": "v2",
                **({"0x-api-key": self.api_key} if self.api_key else {}),
                "User-Agent": "Dragon-Arbitrage/2.0",
            },
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def _request(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("ZEROX_API_KEY is not configured")
        response = self.client.get(f"{self.BASE_URL}/{path}", params=params)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("0x returned a non-object response")
        return payload

    def quote(
        self,
        *,
        chain_id: int,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker: str,
        slippage_bps: int = 50,
        excluded_sources: str | None = None,
    ) -> tuple[DexQuote, DexExecution]:
        started = time.perf_counter()
        params: dict[str, Any] = {
            "chainId": chain_id,
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": sell_amount,
            "taker": taker,
            "slippageBps": slippage_bps,
        }
        if excluded_sources:
            params["excludedSources"] = excluded_sources

        payload = self._request("quote", params)
        if payload.get("liquidityAvailable") is False:
            raise RuntimeError("0x reports no liquidity for this quote")

        issues = payload.get("issues") or {}
        tx = payload.get("transaction") or {}
        if not tx.get("to") or not tx.get("data"):
            raise RuntimeError("0x quote did not return executable transaction calldata")

        latency_ms = Decimal(str((time.perf_counter() - started) * 1000))
        quote = DexQuote(
            chain=str(chain_id),
            venue="0x",
            sell_token=str(payload["sellToken"]),
            buy_token=str(payload["buyToken"]),
            sell_amount=Decimal(str(payload["sellAmount"])),
            buy_amount=Decimal(str(payload["buyAmount"])),
            gas_native=Decimal(str(payload.get("gas", "0"))),
            gas_quote=Decimal("0"),
            fee_bps=Decimal("0"),
            slippage_bps=Decimal(str(slippage_bps)),
            latency_ms=latency_ms,
        )
        execution = DexExecution(
            chain_id=chain_id,
            venue="0x",
            to=str(tx["to"]),
            data=str(tx["data"]),
            value=int(tx.get("value", "0")),
            gas=int(tx["gas"]) if tx.get("gas") is not None else None,
            gas_price=int(tx["gasPrice"]) if tx.get("gasPrice") is not None else None,
            sell_token=str(payload["sellToken"]),
            buy_token=str(payload["buyToken"]),
            sell_amount=int(payload["sellAmount"]),
            buy_amount=int(payload["buyAmount"]),
            allowance_target=((issues.get("allowance") or {}).get("spender") or payload.get("allowanceTarget")),
            issues=issues,
        )
        return quote, execution

    def close(self) -> None:
        self.client.close()
