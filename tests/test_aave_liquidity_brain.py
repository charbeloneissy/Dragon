from decimal import Decimal

from src.dragon.aave_liquidity_brain import (
    best_quote_liquidity,
    build_v4_candidates,
    summarize,
)


def test_v4_brain_caps_liquidity_and_rejects_paused():
    spokes = [
        {
            "address": "0xSpoke",
            "connectedHubs": [
                {
                    "hub": {"address": "0xHub"},
                    "summary": {
                        "creditLine": {"value": "1000"},
                        "creditUsed": {"value": "200"},
                    },
                }
            ],
        }
    ]
    reserves = [
        {
            "id": "r1",
            "onChainId": "1",
            "spoke": {"address": "0xSpoke"},
            "asset": {
                "id": "a1",
                "token": {
                    "address": "0xToken",
                    "symbol": "USDC",
                    "decimals": 6,
                },
            },
            "summary": {
                "supplied": {"amount": {"value": "100"}},
                "borrowed": {"amount": {"value": "50"}},
            },
            "settings": {
                "supplyCap": {"amount": {"value": "700"}},
                "borrowCap": {"amount": {"value": "600"}},
                "borrowable": True,
            },
            "status": {"active": True, "frozen": False, "paused": False},
            "canSupply": True,
            "canBorrow": True,
        },
        {
            "id": "r2",
            "onChainId": "2",
            "spoke": {"address": "0xSpoke"},
            "asset": {
                "id": "a2",
                "token": {
                    "address": "0xPaused",
                    "symbol": "BAD",
                    "decimals": 6,
                },
            },
            "summary": {},
            "settings": {"supplyCap": {"amount": {"value": "1000"}}},
            "status": {"active": True, "frozen": False, "paused": True},
            "canSupply": True,
            "canBorrow": False,
        },
    ]

    candidates = build_v4_candidates(
        spokes=spokes,
        reserves=reserves,
        chain_id=5042,
    )

    usdc = next(row for row in candidates if row.symbol == "USDC")
    assert usdc.execution_ready is True
    assert usdc.liquidity_ceiling == Decimal("600")
    assert usdc.usable is True
    assert best_quote_liquidity(candidates, token="0xToken", quote_decimals=6) == Decimal("600")

    paused = next(row for row in candidates if row.symbol == "BAD")
    assert paused.usable is False

    summary = summarize(candidates)
    assert summary["candidates"] == 2
    assert summary["usable"] == 1
