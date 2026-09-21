from __future__ import annotations

import os

import pytest

from src.dragon.solana_helius_sender import (
    HeliusSender,
    HeliusSenderConfig,
    MIN_SWQOS_TIP_LAMPORTS,
)


def _sender(monkeypatch: pytest.MonkeyPatch) -> HeliusSender:
    monkeypatch.setenv("SOLANA_LIVE_EXECUTION_ENABLED", "true")
    return HeliusSender(
        HeliusSenderConfig(
            api_key="test-key",
            endpoint="https://sender.helius-rpc.com/fast?swqos_only=true",
        )
    )


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOLANA_LIVE_EXECUTION_ENABLED", raising=False)
    sender = HeliusSender(
        HeliusSenderConfig(
            api_key="test-key",
            endpoint="https://sender.helius-rpc.com/fast?swqos_only=true",
        )
    )
    assert sender.enabled is False


def test_rejects_tip_below_swqos_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _sender(monkeypatch)
    with pytest.raises(ValueError, match="5000"):
        sender.send(
            b"tx",
            tip_lamports=MIN_SWQOS_TIP_LAMPORTS - 1,
            simulation_passed=True,
            profit_gate_passed=True,
            risk_gate_passed=True,
        )


def test_rejects_without_all_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    sender = _sender(monkeypatch)
    with pytest.raises(RuntimeError, match="simulation"):
        sender.send(
            b"tx",
            tip_lamports=MIN_SWQOS_TIP_LAMPORTS,
            simulation_passed=False,
            profit_gate_passed=True,
            risk_gate_passed=True,
        )
