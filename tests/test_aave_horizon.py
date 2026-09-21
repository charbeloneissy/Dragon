import pytest

from src.dragon.aave_graphql import AAVE_HORIZON_POOL_ADDRESS, AaveGraphQLClient


class StubHorizonClient(AaveGraphQLClient):
    def __init__(self, payload):
        super().__init__(v3_url="https://example.invalid/graphql")
        self.payload = payload

    async def query_v3(self, query, variables=None):
        assert "market(request:" in query
        assert variables["request"]["address"] == AAVE_HORIZON_POOL_ADDRESS
        assert variables["request"]["chainId"] == 1
        return self.payload


@pytest.mark.asyncio
async def test_horizon_market_uses_v3_request_shape():
    payload = {
        "market": {
            "name": "Aave Horizon",
            "chain": {"name": "Ethereum", "chainId": 1},
            "address": AAVE_HORIZON_POOL_ADDRESS,
            "totalMarketSize": "1",
            "totalAvailableLiquidity": "1",
            "reserves": [],
        }
    }
    client = StubHorizonClient(payload)
    data = await client.horizon_market()
    assert data["name"] == "Aave Horizon"
    assert data["chain"]["chainId"] == 1


@pytest.mark.asyncio
async def test_horizon_market_returns_none_when_market_missing():
    client = StubHorizonClient({})
    assert await client.horizon_market() is None
