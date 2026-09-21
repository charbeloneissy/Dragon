from decimal import Decimal

import pytest

from src.dragon.aave_credit_delegation import (
    CreditDelegationSnapshot,
    validate_credit_delegation_request,
)


def test_credit_delegation_rejects_incompatible_emode():
    with pytest.raises(ValueError, match="eMode"):
        validate_credit_delegation_request(
            pool="0x0000000000000000000000000000000000000001",
            asset="0x0000000000000000000000000000000000000002",
            delegator="0x0000000000000000000000000000000000000010",
            delegatee="0x0000000000000000000000000000000000000020",
            amount=1,
            delegator_emode_category=1,
            borrow_asset_emode_category=2,
        )


def test_credit_delegation_snapshot_health_factor():
    snapshot = CreditDelegationSnapshot(
        pool="0x0000000000000000000000000000000000000001",
        asset="0x0000000000000000000000000000000000000002",
        variable_debt_token="0x0000000000000000000000000000000000000003",
        delegator="0x0000000000000000000000000000000000000010",
        delegatee="0x0000000000000000000000000000000000000020",
        allowance=500,
        total_collateral_base=1000,
        total_debt_base=250,
        available_borrows_base=500,
        health_factor_wad=int(Decimal("2.5") * 10**18),
        emode_category=1,
    )
    assert snapshot.healthy is True
    assert snapshot.health_factor == Decimal("2.5")
    assert snapshot.as_dict()["allowance"] == "500"


def test_credit_delegation_rejects_self_delegation():
    with pytest.raises(ValueError, match="differ"):
        validate_credit_delegation_request(
            pool="0x0000000000000000000000000000000000000001",
            asset="0x0000000000000000000000000000000000000002",
            delegator="0x0000000000000000000000000000000000000010",
            delegatee="0x0000000000000000000000000000000000000010",
            amount=1,
        )
