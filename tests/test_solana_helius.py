from __future__ import annotations

import asyncio
import os

from src.dragon.solana_helius import (
    HeliusWebSocketStream,
    SolanaWorldStateBrain,
    SolanaWorldState,
)


def test_helius_stream_fails_closed_without_key(monkeypatch):
    monkeypatch.setenv("SOLANA_HELIUS_ENABLED", "true")
    monkeypatch.delenv("HELIUS_API_KEY", raising=False)
    stream = HeliusWebSocketStream()
    assert stream.enabled is False


def test_world_state_consumes_slot():
    brain = SolanaWorldStateBrain(SolanaWorldState())
    observation = asyncio.run(brain.consume({
        "jsonrpc": "2.0",
        "method": "slotNotification",
        "params": {
            "result": {"parent": 99, "slot": 100},
            "subscription": 1,
        },
    }))
    assert observation is not None
    assert observation.kind == "slot"
    assert brain.state.slot == 100
    assert brain.state.freshness_ms() >= 0


def test_world_state_consumes_transaction():
    brain = SolanaWorldStateBrain()
    observation = asyncio.run(brain.consume({
        "jsonrpc": "2.0",
        "method": "transactionNotification",
        "params": {
            "result": {
                "context": {"slot": 123},
                "value": {
                    "transaction": {"signatures": ["sig-123"]},
                },
            },
            "subscription": 2,
        },
    }))
    assert observation is not None
    assert observation.kind == "transaction"
    assert brain.state.slot == 123
    assert brain.state.transactions_seen == 1
    assert brain.state.last_signature == "sig-123"
