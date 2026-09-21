from __future__ import annotations

"""Helius-powered Solana real-time data adapter for Dragon.

This module is intentionally data-plane only:
- it never signs transactions;
- it never submits transactions;
- it fails closed when the Helius key is absent;
- it normalizes slot/account/transaction observations into a small World State
  surface that the common Dragon brains can consume.

Enhanced Helius WebSockets are used first because they require no gRPC runtime
or protobuf schema in Dragon. The adapter can later be swapped to LaserStream
gRPC without changing the World State consumer.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import websockets


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolanaObservation:
    kind: str
    slot: int
    observed_at_ms: int
    payload: dict[str, Any]


@dataclass
class SolanaWorldState:
    slot: int = 0
    last_observed_at_ms: int = 0
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    transactions_seen: int = 0
    last_signature: str | None = None
    last_error: str | None = None

    def freshness_ms(self, now_ms: int | None = None) -> int:
        if self.last_observed_at_ms <= 0:
            return 2**31 - 1
        now = int(time.time() * 1000) if now_ms is None else int(now_ms)
        return max(0, now - self.last_observed_at_ms)

    def snapshot(self) -> dict[str, Any]:
        return {
            "chain": "solana",
            "slot": self.slot,
            "last_observed_at_ms": self.last_observed_at_ms,
            "freshness_ms": self.freshness_ms(),
            "accounts_tracked": len(self.accounts),
            "transactions_seen": self.transactions_seen,
            "last_signature": self.last_signature,
            "last_error": self.last_error,
        }


def _slot_from_message(message: dict[str, Any]) -> int:
    params = message.get("params") or {}
    result = params.get("result")
    if isinstance(result, dict):
        context = result.get("context")
        if isinstance(context, dict):
            try:
                return int(context.get("slot", 0))
            except (TypeError, ValueError):
                pass
        value = result.get("value")
        if isinstance(value, dict):
            try:
                return int(value.get("slot", 0))
            except (TypeError, ValueError):
                pass
    return 0


def _signature_from_transaction(message: dict[str, Any]) -> str | None:
    params = message.get("params") or {}
    result = params.get("result")
    if not isinstance(result, dict):
        return None
    value = result.get("value", result)
    if not isinstance(value, dict):
        return None
    tx = value.get("transaction")
    if isinstance(tx, dict):
        signatures = tx.get("signatures")
        if isinstance(signatures, list) and signatures:
            return str(signatures[0])
    signature = value.get("signature")
    return str(signature) if signature else None


class HeliusWebSocketStream:
    """Reconnectable Helius LaserStream-powered WebSocket client."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        ws_url: str | None = None,
        commitment: str | None = None,
        reconnect_seconds: float | None = None,
        max_reconnect_seconds: float | None = None,
    ) -> None:
        self.api_key = (api_key or os.getenv("HELIUS_API_KEY", "")).strip()
        self.ws_url = (ws_url or os.getenv(
            "HELIUS_WSS_URL",
            "wss://mainnet.helius-rpc.com/",
        )).strip()
        self.commitment = (commitment or os.getenv(
            "SOLANA_COMMITMENT", "processed"
        )).strip()
        self.reconnect_seconds = max(
            0.25,
            float(reconnect_seconds or os.getenv(
                "SOLANA_WSS_RECONNECT_SECONDS", "0.5"
            )),
        )
        self.max_reconnect_seconds = max(
            self.reconnect_seconds,
            float(max_reconnect_seconds or os.getenv(
                "SOLANA_WSS_MAX_RECONNECT_SECONDS", "10"
            )),
        )
        self._stop = asyncio.Event()
        self._request_id = 0

    @property
    def enabled(self) -> bool:
        return bool(self.api_key) and os.getenv(
            "SOLANA_HELIUS_ENABLED", "false"
        ).strip().lower() in {"1", "true", "yes", "on"}

    def _endpoint(self) -> str:
        if not self.api_key:
            raise RuntimeError("HELIUS_API_KEY is not configured")
        separator = "&" if "?" in self.ws_url else "?"
        return f"{self.ws_url}{separator}api-key={self.api_key}"

    def stop(self) -> None:
        self._stop.set()

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _subscription(
        self,
        method: str,
        params: list[Any],
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }

    async def _send_subscriptions(
        self,
        ws: Any,
        *,
        account_include: list[str],
        transaction_accounts: list[str],
    ) -> None:
        await ws.send(json.dumps(self._subscription(
            "slotSubscribe", []
        )))
        for account in account_include:
            await ws.send(json.dumps(self._subscription(
                "accountSubscribe",
                [account, {"commitment": self.commitment, "encoding": "base64"}],
            )))
        if transaction_accounts:
            await ws.send(json.dumps(self._subscription(
                "transactionSubscribe",
                [
                    {
                        "failed": False,
                        "accountInclude": transaction_accounts,
                    },
                    {
                        "commitment": self.commitment,
                        "encoding": "jsonParsed",
                        "transactionDetails": "full",
                        "maxSupportedTransactionVersion": 0,
                    },
                ],
            )))

    async def run(
        self,
        on_message: Callable[[dict[str, Any]], Awaitable[None]],
        *,
        account_include: list[str] | None = None,
        transaction_accounts: list[str] | None = None,
    ) -> None:
        if not self.enabled:
            LOGGER.info(
                "Solana Helius stream disabled; set SOLANA_HELIUS_ENABLED=true "
                "and HELIUS_API_KEY in Render"
            )
            return

        accounts = list(account_include or [])
        tx_accounts = list(transaction_accounts or [])
        delay = self.reconnect_seconds

        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    self._endpoint(),
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=2,
                    max_size=16 * 1024 * 1024,
                ) as ws:
                    LOGGER.info(
                        "Helius Solana stream connected commitment=%s",
                        self.commitment,
                    )
                    await self._send_subscriptions(
                        ws,
                        account_include=accounts,
                        transaction_accounts=tx_accounts,
                    )
                    delay = self.reconnect_seconds
                    async for raw in ws:
                        if self._stop.is_set():
                            break
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        try:
                            message = json.loads(raw)
                        except json.JSONDecodeError:
                            LOGGER.warning("ignored non-JSON Helius message")
                            continue
                        await on_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Helius Solana stream disconnected: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                if self._stop.is_set():
                    break
                await asyncio.sleep(delay)
                delay = min(self.max_reconnect_seconds, delay * 2)


class SolanaWorldStateBrain:
    """Normalize Helius notifications into the common Dragon World State."""

    def __init__(self, state: SolanaWorldState | None = None) -> None:
        self.state = state or SolanaWorldState()

    async def consume(self, message: dict[str, Any]) -> SolanaObservation | None:
        method = str(message.get("method", ""))
        now_ms = int(time.time() * 1000)
        slot = _slot_from_message(message)
        if slot:
            self.state.slot = max(self.state.slot, slot)
        self.state.last_observed_at_ms = now_ms
        self.state.last_error = None

        if method == "slotNotification":
            return SolanaObservation("slot", self.state.slot, now_ms, message)

        if method == "accountNotification":
            params = message.get("params") or {}
            result = params.get("result") or {}
            value = result.get("value") if isinstance(result, dict) else None
            subscription = params.get("subscription")
            key = str(subscription or "unknown")
            self.state.accounts[key] = {
                "slot": slot,
                "value": value,
                "observed_at_ms": now_ms,
            }
            return SolanaObservation("account", self.state.slot, now_ms, message)

        if method in {"transactionNotification", "parsedTransactionNotification"}:
            self.state.transactions_seen += 1
            self.state.last_signature = _signature_from_transaction(message)
            return SolanaObservation("transaction", self.state.slot, now_ms, message)

        return SolanaObservation(method or "unknown", self.state.slot, now_ms, message)
