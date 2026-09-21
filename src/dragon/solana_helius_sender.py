from __future__ import annotations

"""Helius Sender SWQOS-only transport for Dragon.

Transport only: this module never creates keys, signs transactions, or decides
whether an opportunity is profitable. The caller must provide a fully signed
serialized Solana transaction plus successful Dragon risk/simulation gates.
"""

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


MIN_SWQOS_TIP_LAMPORTS = 5_000


@dataclass(frozen=True)
class HeliusSenderConfig:
    api_key: str
    endpoint: str
    min_tip_lamports: int = MIN_SWQOS_TIP_LAMPORTS
    skip_preflight: bool = True
    max_retries: int = 0

    @classmethod
    def from_env(cls) -> "HeliusSenderConfig":
        key = os.getenv("HELIUS_API_KEY", "").strip()
        if not key:
            raise RuntimeError("HELIUS_API_KEY is not configured")
        endpoint = os.getenv(
            "HELIUS_SENDER_ENDPOINT",
            "https://sender.helius-rpc.com/fast?swqos_only=true",
        ).strip()
        return cls(api_key=key, endpoint=endpoint)


@dataclass(frozen=True)
class SenderResult:
    signature: str
    endpoint: str
    tip_lamports: int
    skip_preflight: bool
    max_retries: int


class HeliusSender:
    """Send an already-signed transaction through Helius Sender SWQOS-only."""

    def __init__(
        self,
        config: HeliusSenderConfig | None = None,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        self.config = config or HeliusSenderConfig.from_env()
        self.timeout_seconds = max(0.5, float(timeout_seconds))

    @property
    def enabled(self) -> bool:
        return (
            bool(self.config.api_key)
            and os.getenv("SOLANA_LIVE_EXECUTION_ENABLED", "false")
            .strip()
            .lower()
            in {"1", "true", "yes", "on"}
        )

    def _url(self) -> str:
        endpoint = self.config.endpoint
        separator = "&" if "?" in endpoint else "?"
        if "swqos_only=" not in endpoint:
            endpoint += separator + "swqos_only=true"
        separator = "&" if "?" in endpoint else "?"
        if "api-key=" not in endpoint:
            endpoint += separator + f"api-key={self.config.api_key}"
        return endpoint

    @staticmethod
    def _validate_serialized_transaction(serialized: bytes) -> str:
        if not serialized:
            raise ValueError("serialized transaction is empty")
        # Base64 round-trip validation catches malformed transport payloads
        # without requiring a Solana signing/key dependency in this adapter.
        encoded = base64.b64encode(serialized).decode("ascii")
        if base64.b64decode(encoded) != serialized:
            raise ValueError("transaction serialization failed")
        return encoded

    def send(
        self,
        serialized_transaction: bytes,
        *,
        tip_lamports: int,
        simulation_passed: bool,
        profit_gate_passed: bool,
        risk_gate_passed: bool,
    ) -> SenderResult:
        if not self.enabled:
            raise RuntimeError("Solana live execution is disabled")
        if tip_lamports < self.config.min_tip_lamports:
            raise ValueError(
                f"Sender SWQOS tip must be >= {self.config.min_tip_lamports} lamports"
            )
        if not simulation_passed:
            raise RuntimeError("refusing Sender submission: simulation gate failed")
        if not profit_gate_passed:
            raise RuntimeError("refusing Sender submission: profit gate failed")
        if not risk_gate_passed:
            raise RuntimeError("refusing Sender submission: risk gate failed")

        encoded = self._validate_serialized_transaction(serialized_transaction)
        request = {
            "jsonrpc": "2.0",
            "id": "dragon-sender",
            "method": "sendTransaction",
            "params": [
                encoded,
                {
                    "encoding": "base64",
                    "skipPreflight": self.config.skip_preflight,
                    "maxRetries": self.config.max_retries,
                },
            ],
        }

        headers = {"Content-Type": "application/json"}
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.post(self._url(), headers=headers, content=json.dumps(request))
            response.raise_for_status()
            payload: dict[str, Any] = response.json()

        if payload.get("error"):
            error = payload["error"]
            raise RuntimeError(f"Helius Sender rejected transaction: {error}")
        signature = payload.get("result")
        if not isinstance(signature, str) or not signature:
            raise RuntimeError("Helius Sender returned no transaction signature")

        return SenderResult(
            signature=signature,
            endpoint=self.config.endpoint,
            tip_lamports=tip_lamports,
            skip_preflight=self.config.skip_preflight,
            max_retries=self.config.max_retries,
        )
