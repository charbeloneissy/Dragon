import asyncio
from decimal import Decimal

import pytest

from src.dragon.aave_agent import AaveAgentPolicy
from src.dragon.aave_mcp import canonical_human_amount, parse_human_amount


def test_human_amount_rejects_exponent_and_hex():
    with pytest.raises(ValueError):
        parse_human_amount("1e5")
    with pytest.raises(ValueError):
        parse_human_amount("0x10")


def test_human_amount_is_canonical_plain_decimal():
    assert canonical_human_amount("10.5000") == "10.5"
    assert canonical_human_amount("100000") == "100000"


def test_execution_plan_summary_distinguishes_approval_and_two_step():
    policy = AaveAgentPolicy()
    approval = policy.execution_plan_summary({
        "__typename": "Erc20ApprovalRequired",
        "byTransaction": {"to": "0x1"},
        "bySignature": {"domain": {"name": "Permit"}},
    })
    assert approval["ready"] is False
    assert approval["requires_approval"] is True
    assert approval["permit_available"] is True

    gateway = policy.execution_plan_summary({
        "__typename": "PreContractActionRequired",
        "transaction": {"to": "0x1"},
        "originalTransaction": {"to": "0x2"},
    })
    assert gateway["requires_ordered_steps"] is True
    assert gateway["first_step_present"] is True
    assert gateway["second_step_present"] is True


def test_execution_plan_summary_requires_complete_transaction_request():
    policy = AaveAgentPolicy()
    incomplete = policy.execution_plan_summary({
        "__typename": "TransactionRequest",
        "to": "0x1",
        "from": "0x2",
        "data": "0x",
        "chainId": 1,
    })
    assert incomplete["ready"] is False
    assert "value" in incomplete["missing_fields"]
