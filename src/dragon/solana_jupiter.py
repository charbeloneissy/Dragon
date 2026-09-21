from __future__ import annotations

"""Read-only Jupiter quote adapter for Dragon's Solana engine.

No transaction building, signing, or submission occurs here. Quotes are
inputs to the common opportunity/risk brains and must pass freshness and
profit gates before they can be considered executable.
"""

import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx


@dataclass(frozen=True)
class JupiterQuote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: Decimal
    route_plan: tuple[dict[str, Any], ...]
    context_slot: int | None
    fetched_at_ms: int
    raw: dict[str, Any]

    @property
    def age_ms(self) -> int:
        return max(0, int(time.time() * 1000) - self.fetched_at_ms)


class JupiterQuoteAdapter:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float = 2.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("JUPITER_API_KEY", "")).strip()
        self.base_url = (
            base_url
            or os.getenv("JUPITER_QUOTE_URL", "https://api.jup.ag/swap/v1/quote")
        ).strip()
        self.timeout_seconds = max(0.25, float(timeout_seconds))

    @property
    def enabled(self) -> bool:
        return bool(self.api_key or os.getenv("JUPITER_QUOTE_ALLOW_UNKEYED", "false").lower() in {"1", "true", "yes", "on"})

    def quote(
        self,
        *,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
        only_direct_routes: bool = False,
    ) -> JupiterQuote:
        if not self.enabled:
            raise RuntimeError("Jupiter quote adapter disabled: configure JUPITER_API_KEY or explicitly allow unkeyed quotes")
        if not input_mint or not output_mint or input_mint == output_mint:
            raise ValueError("input_mint and output_mint must be distinct")
        if amount <= 0:
            raise ValueError("amount must be positive")
        if not 0 <= slippage_bps <= 5000:
            raise ValueError("slippage_bps must be between 0 and 5000")

        params: dict[str, Any] = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(slippage_bps),
            "restrictIntermediateTokens": "true",
        }
        if only_direct_routes:
            params["onlyDirectRoutes"] = "true"

        headers = {"accept": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key

        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(self.base_url, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()

        if not isinstance(payload, dict):
            raise ValueError("Jupiter quote response is not an object")
        out_raw = payload.get("outAmount")
        if out_raw is None:
            raise ValueError(f"Jupiter quote missing outAmount: {payload}")
        try:
            out_amount = int(out_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("Jupiter outAmount is not an integer") from exc
        if out_amount <= 0:
            raise ValueError("Jupiter returned non-positive outAmount")

        impact_raw = payload.get("priceImpactPct", "0")
        try:
            impact = Decimal(str(impact_raw))
        except Exception as exc:
            raise ValueError("invalid Jupiter priceImpactPct") from exc
        if impact < 0:
            raise ValueError("negative Jupiter price impact")

        context_slot = payload.get("contextSlot")
        try:
            context_slot = int(context_slot) if context_slot is not None else None
        except (TypeError, ValueError):
            context_slot = None

        route_plan = payload.get("routePlan") or []
        if not isinstance(route_plan, list):
            route_plan = []

        return JupiterQuote(
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=amount,
            out_amount=out_amount,
            price_impact_pct=impact,
            route_plan=tuple(x for x in route_plan if isinstance(x, dict)),
            context_slot=context_slot,
            fetched_at_ms=int(time.time() * 1000),
            raw=payload,
        )
