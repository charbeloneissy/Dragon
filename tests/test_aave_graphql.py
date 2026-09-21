import asyncio

from src.dragon.aave_graphql import (
    AAVE_V4_ARC_CHAIN_ID,
    AaveGraphQLClient,
)


def test_arc_chain_detection(monkeypatch):
    client = AaveGraphQLClient()

    async def fake_chains():
        return [
            {"name": "Ethereum", "chainId": 1},
            {"name": "Arc", "chainId": AAVE_V4_ARC_CHAIN_ID},
        ]

    client.chains = fake_chains
    row = asyncio.run(client.arc_chain())
    assert row["name"] == "Arc"
    assert row["chainId"] == 5042


def test_processed_transaction_result(monkeypatch):
    client = AaveGraphQLClient()

    async def fake_query(query, variables=None):
        assert "hasProcessedKnownTransaction" in query
        assert variables["operations"] == ["SUPPLY"]
        assert variables["txHash"].startswith("0x")
        return {"value": True}

    client.query = fake_query
    result = asyncio.run(
        client.has_processed_known_transaction(
            operations=["SUPPLY"],
            tx_hash="0x" + "11" * 32,
        )
    )
    assert result is True


def test_transaction_query_is_raw_and_unsigned():
    client = AaveGraphQLClient()

    async def fake_query(query, variables=None):
        return {
            "vaultSetFee": {
                "to": "0x1234567890abcdef1234567890abcdef12345678",
                "from": "0x1111111111111111111111111111111111111111",
                "data": "0x1234",
                "value": "0",
                "chainId": 5042,
            }
        }

    client.query = fake_query
    result = asyncio.run(client.transaction_query("mutation Foo { foo }"))
    assert result["vaultSetFee"]["chainId"] == 5042
    assert result["vaultSetFee"]["data"] == "0x1234"


def test_spokes_and_reserves_and_assets_are_typed_lists(monkeypatch):
    client = AaveGraphQLClient()
    calls = []

    async def fake_query(query, variables=None):
        calls.append((query, variables))
        if "query Spokes" in query:
            return {"spokes": [{"id": "s1", "name": "Arc Main"}]}
        if "query Reserves" in query:
            return {"value": [{"id": "r1", "onChainId": "1"}]}
        if "query Asset(" in query:
            return {"value": {"id": "a1", "token": {"symbol": "USDC"}}}
        if "query MultichainAsset(" in query:
            return {"value": {"summary": {"chainCount": 2}}}
        raise AssertionError("unexpected query")

    client.query = fake_query
    spokes = asyncio.run(client.spokes({"query": {"chainIds": [5042]}}))
    reserves = asyncio.run(client.reserves({"query": {"chainIds": [5042]}}))
    asset = asyncio.run(client.asset({"query": {"token": {"address": "0x" + "11" * 20, "chainId": 5042}}}))
    multi = asyncio.run(client.multichain_asset({"query": {"symbol": "USDC"}}))

    assert spokes[0]["id"] == "s1"
    assert reserves[0]["id"] == "r1"
    assert asset["id"] == "a1"
    assert multi["summary"]["chainCount"] == 2
    assert len(calls) == 4


def test_preview_update_user_position_conditions():
    client = AaveGraphQLClient()

    async def fake_query(query, variables=None):
        assert "query Preview" in query
        assert variables["request"]["action"]["updateUserPositionConditions"]["update"] == "ALL_DYNAMIC_CONFIG"
        assert variables["request"]["action"]["updateUserPositionConditions"]["userPositionId"] == "UP-1"
        return {
            "value": {
                "id": "UP-1",
                "riskPremium": {
                    "before": {"normalized": "1.0"},
                    "after": {"normalized": "1.0"},
                },
                "otherConditions": [],
            }
        }

    client.query = fake_query
    result = asyncio.run(
        client.preview(
            {
                "updateUserPositionConditions": {
                    "userPositionId": "UP-1",
                    "update": "ALL_DYNAMIC_CONFIG",
                }
            }
        )
    )
    assert result["id"] == "UP-1"


def test_update_user_position_conditions_tx_is_unsigned():
    client = AaveGraphQLClient()

    async def fake_query(query, variables=None):
        assert "query UpdateUserPositionConditions" in query
        assert variables["request"]["update"] == "ALL_DYNAMIC_CONFIG"
        return {
            "value": {
                "to": "0x1234567890abcdef1234567890abcdef12345678",
                "from": "0x1111111111111111111111111111111111111111",
                "data": "0xdeadbeef",
                "value": "0",
                "chainId": 5042,
                "operations": ["UPDATE_USER_POSITION_CONDITIONS"],
            }
        }

    client.query = fake_query
    result = asyncio.run(
        client.update_user_position_conditions_tx(
            user_position_id="UP-1",
            update="ALL_DYNAMIC_CONFIG",
        )
    )
    assert result["chainId"] == 5042
    assert result["data"] == "0xdeadbeef"
