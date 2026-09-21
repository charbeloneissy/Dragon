from __future__ import annotations

"""Background Solana World State bridge for the Dragon runner."""

import asyncio
import logging
import os
import time
from threading import Lock
from typing import Any

from .solana_helius import HeliusWebSocketStream, SolanaWorldStateBrain

LOGGER = logging.getLogger(__name__)


def _accounts_from_env() -> list[str]:
    raw = os.getenv("SOLANA_TX_ACCOUNT_INCLUDE", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


async def run_solana_world_state(state: dict[str, Any], lock: Lock) -> None:
    stream = HeliusWebSocketStream()
    brain = SolanaWorldStateBrain()
    accounts = _accounts_from_env()

    with lock:
        state["solana_world_state"] = {
            "enabled": stream.enabled,
            "status": "starting" if stream.enabled else "disabled",
            **brain.state.snapshot(),
        }

    async def consume(message: dict[str, Any]) -> None:
        observation = await brain.consume(message)
        if observation is None:
            return
        with lock:
            state["solana_world_state"] = {
                "enabled": True,
                "status": "running",
                "last_event": observation.kind,
                **brain.state.snapshot(),
            }

    try:
        await stream.run(
            consume,
            transaction_accounts=accounts,
        )
    except asyncio.CancelledError:
        stream.stop()
        with lock:
            state["solana_world_state"] = {
                "enabled": stream.enabled,
                "status": "stopped",
                **brain.state.snapshot(),
            }
        raise
    except Exception as exc:
        LOGGER.exception("Solana World State stopped")
        brain.state.last_error = f"{type(exc).__name__}: {exc}"
        with lock:
            state["solana_world_state"] = {
                "enabled": stream.enabled,
                "status": "error",
                **brain.state.snapshot(),
            }


def solana_state_snapshot(state: dict[str, Any], lock: Lock) -> dict[str, Any]:
    with lock:
        return dict(state.get("solana_world_state", {}))
