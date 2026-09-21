from __future__ import annotations

"""Background Solana quote loop feeding Dragon World State."""

import asyncio
import logging
import os
import time
from threading import Lock
from typing import Any

from .solana_jupiter import JupiterQuoteAdapter

LOGGER = logging.getLogger(__name__)


def _pairs() -> list[tuple[str, str]]:
    rows = []
    for item in os.getenv("SOLANA_QUOTE_PAIRS", "").split(","):
        item = item.strip()
        if not item:
            continue
        parts = [x.strip() for x in item.split(":", 1)]
        if len(parts) == 2 and parts[0] and parts[1] and parts[0] != parts[1]:
            rows.append((parts[0], parts[1]))
    return rows


def _amounts() -> list[int]:
    result = []
    for item in os.getenv("SOLANA_QUOTE_AMOUNTS", "").split(","):
        try:
            amount = int(item.strip())
        except ValueError:
            continue
        if amount > 0:
            result.append(amount)
    return result or [1_000_000]


async def run_solana_quotes(state: dict[str, Any], lock: Lock) -> None:
    adapter = JupiterQuoteAdapter()
    pairs = _pairs()
    amounts = _amounts()
    interval = max(0.25, float(os.getenv("SOLANA_QUOTE_POLL_SECONDS", "0.5")))
    slippage = max(0, min(5000, int(os.getenv("SOLANA_QUOTE_SLIPPAGE_BPS", "50"))))

    with lock:
        state["solana_quotes"] = {
            "enabled": adapter.enabled and bool(pairs),
            "status": "running" if adapter.enabled and pairs else "disabled",
            "pairs_configured": len(pairs),
            "quotes": [],
            "last_update_ms": 0,
            "error": None,
        }

    if not adapter.enabled or not pairs:
        return

    while True:
        started = int(time.time() * 1000)
        rows = []
        errors = []
        for input_mint, output_mint in pairs:
            for amount in amounts:
                try:
                    quote = await asyncio.to_thread(
                        adapter.quote,
                        input_mint=input_mint,
                        output_mint=output_mint,
                        amount=amount,
                        slippage_bps=slippage,
                    )
                    rows.append({
                        "input_mint": quote.input_mint,
                        "output_mint": quote.output_mint,
                        "in_amount": quote.in_amount,
                        "out_amount": quote.out_amount,
                        "price_impact_pct": str(quote.price_impact_pct),
                        "context_slot": quote.context_slot,
                        "age_ms": quote.age_ms,
                        "route_hops": len(quote.route_plan),
                    })
                except Exception as exc:
                    errors.append(
                        f"{input_mint}->{output_mint}:{type(exc).__name__}:{exc}"
                    )
        with lock:
            state["solana_quotes"] = {
                "enabled": True,
                "status": "running",
                "pairs_configured": len(pairs),
                "quotes": rows[:100],
                "last_update_ms": started,
                "loop_ms": max(0, int(time.time() * 1000) - started),
                "error": errors[-5:] or None,
            }
        await asyncio.sleep(interval)
