from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from .dex import DexQuote

_ADDRESS_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


@dataclass(frozen=True)
class DexExecution:
    chain_id: int
    venue: str
    source: str
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
    """0x Swap API v2 quote/calldata adapter.

    Quotes are advisory. This class never signs or broadcasts transactions.
    """

    BASE_URL = "https://api.0x.org/swap/allowance-holder"
    SOURCES_URL = "https://api.0x.org/sources"

    def __init__(self, api_key: str | None = None, timeout: float = 4.0):
        self.api_key = (api_key or os.getenv("ZEROX_API_KEY", "")).strip()
        self.timeout = float(timeout)
        self.client = httpx.Client(
            timeout=self.timeout,
            headers={
                "Accept": "application/json",
                "0x-version": "v2",
                **({"0x-api-key": self.api_key} if self.api_key else {}),
                "User-Agent": "Dragon-Arbitrage/2.1",
            },
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def _validate_address(name: str, value: str) -> None:
        if not _ADDRESS_RE.fullmatch(value):
            raise ValueError(f"{name} must be a valid EVM address")

    @staticmethod
    def _validate_amount(sell_amount: int) -> None:
        if isinstance(sell_amount, bool) or int(sell_amount) <= 0:
            raise ValueError("sell_amount must be a positive integer")

    @staticmethod
    def _validate_slippage(slippage_bps: int) -> None:
        if isinstance(slippage_bps, bool) or not 0 <= int(slippage_bps) <= 10_000:
            raise ValueError("slippage_bps must be between 0 and 10000")

    def _get(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled:
            raise RuntimeError("ZEROX_API_KEY is not configured")
        try:
            response = self.client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"0x API request failed: {exc}") from exc
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("0x returned a non-object response")
        return payload

    def sources(self, chain_id: int) -> tuple[str, ...]:
        if int(chain_id) <= 0:
            raise ValueError("chain_id must be positive")
        payload = self._get(self.SOURCES_URL, {"chainId": int(chain_id)})
        values = payload.get("sources") or []
        return tuple(str(value).strip() for value in values if str(value).strip())

    def quote_single_source(
        self,
        *,
        chain_id: int,
        sell_token: str,
        buy_token: str,
        sell_amount: int,
        taker: str,
        source: str,
        slippage_bps: int = 50,
    ) -> tuple[DexQuote, DexExecution]:
        self._validate_address("sell_token", sell_token)
        self._validate_address("buy_token", buy_token)
        self._validate_address("taker", taker)
        if sell_token.lower() == buy_token.lower():
            raise ValueError("sell_token and buy_token must differ")
        if not source.strip():
            raise ValueError("source is required")
        available = set(self.sources(chain_id))
        if source not in available:
            raise ValueError(f"unsupported 0x liquidity source for chain {chain_id}: {source}")
        excluded = ",".join(sorted(available - {source}))
        return self.quote(
            chain_id=chain_id,
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker=taker,
            slippage_bps=slippage_bps,
            excluded_sources=excluded,
            expected_source=source,
        )

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
        expected_source: str | None = None,
    ) -> tuple[DexQuote, DexExecution]:
        self._validate_address("sell_token", sell_token)
        self._validate_address("buy_token", buy_token)
        self._validate_address("taker", taker)
        self._validate_amount(sell_amount)
        self._validate_slippage(slippage_bps)
        if sell_token.lower() == buy_token.lower():
            raise ValueError("sell_token and buy_token must differ")

        started = time.perf_counter()
        params: dict[str, Any] = {
            "chainId": int(chain_id),
            "sellToken": sell_token,
            "buyToken": buy_token,
            "sellAmount": int(sell_amount),
            "taker": taker,
            "slippageBps": int(slippage_bps),
        }
        if excluded_sources:
            params["excludedSources"] = excluded_sources

        payload = self._get(f"{self.BASE_URL}/quote", params)
        if payload.get("liquidityAvailable") is False:
            raise RuntimeError("0x reports no liquidity for this quote")

        issues = payload.get("issues") or {}
        if issues.get("simulationIncomplete"):
            raise RuntimeError("0x quote simulation is incomplete")
        if issues.get("balance"):
            raise RuntimeError("taker has insufficient balance for this quote")

        tx = payload.get("transaction") or {}
        route = payload.get("route") or {}
        fills = route.get("fills") or []
        actual_sources = {str(fill.get("source")) for fill in fills if fill.get("source")}
        if not actual_sources:
            raise RuntimeError("0x quote returned no route source")
        if expected_source and actual_sources != {expected_source}:
            raise RuntimeError(
                f"route is not single-source: expected {expected_source}, got {sorted(actual_sources)}"
            )
        if not tx.get("to") or not tx.get("data"):
            raise RuntimeError("0x quote did not return executable transaction calldata")
        self._validate_address("transaction.to", str(tx["to"]))
        if not isinstance(tx.get("data"), str) or not tx["data"].startswith("0x"):
            raise RuntimeError("0x transaction calldata is invalid")

        buy_amount = int(payload.get("buyAmount", 0))
        returned_sell_amount = int(payload.get("sellAmount", sell_amount))
        if returned_sell_amount != int(sell_amount):
            raise RuntimeError("0x returned a different sell amount than requested")
        if buy_amount <= 0:
            raise RuntimeError("0x returned a non-positive buy amount")

        gas_fee = ((payload.get("fees") or {}).get("gasFee") or {}).get("amount")
        gas_quote = Decimal(str(gas_fee or "0"))
        latency_ms = Decimal(str((time.perf_counter() - started) * 1000))
        venue = next(iter(actual_sources)) if len(actual_sources) == 1 else "mixed"

        quote = DexQuote(
            chain=str(chain_id),
            venue=venue,
            sell_token=str(payload["sellToken"]),
            buy_token=str(payload["buyToken"]),
            sell_amount=Decimal(str(payload["sellAmount"])),
            buy_amount=Decimal(str(payload["buyAmount"])),
            gas_native=Decimal(str(payload.get("totalNetworkFee", "0"))),
            gas_quote=gas_quote,
            fee_bps=Decimal("0"),
            slippage_bps=Decimal(str(slippage_bps)),
            latency_ms=latency_ms,
        )
        execution = DexExecution(
            chain_id=int(chain_id),
            venue="0x",
            source=venue,
            to=str(tx["to"]),
            data=str(tx["data"]),
            value=int(tx.get("value", "0")),
            gas=int(tx["gas"]) if tx.get("gas") is not None else None,
            gas_price=int(tx["gasPrice"]) if tx.get("gasPrice") is not None else None,
            sell_token=str(payload["sellToken"]),
            buy_token=str(payload["buyToken"]),
            sell_amount=int(payload["sellAmount"]),
            buy_amount=buy_amount,
            allowance_target=((issues.get("allowance") or {}).get("spender") or payload.get("allowanceTarget")),
            issues=issues,
        )
        return quote, execution

    def close(self) -> None:
        self.client.close()
