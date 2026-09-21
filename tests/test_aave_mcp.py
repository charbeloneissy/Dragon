from decimal import Decimal

from src.dragon.aave_mcp import parse_markets, rank_stablecoin_supply


def test_parse_v3_and_v4_stable_markets():
    payload = {
        "data": {
            "markets": [
                {
                    "version": "v3",
                    "chainId": 1,
                    "chain": "Ethereum",
                    "symbol": "USDC",
                    "reserveId": "v3-usdc-eth",
                    "supplyAPY": "4.25",
                    "availableLiquidity": "1000000",
                    "utilization": "62.5",
                    "frozen": False,
                    "paused": False,
                    "rewards": [],
                },
                {
                    "version": "v4",
                    "chainId": 1,
                    "chain": "Ethereum",
                    "symbol": "USDC",
                    "reserveId": "v4-usdc-eth",
                    "supplyApy": "5.10",
                    "availableLiquidity": "2000000",
                    "utilization": "55",
                    "rewards": [
                        {"extraApy": "1.25"},
                    ],
                },
                {
                    "version": "v4",
                    "chainId": 43114,
                    "chain": "Avalanche",
                    "symbol": "USDT",
                    "reserveId": "v4-usdt-avax",
                    "supplyApy": "9.0",
                    "availableLiquidity": "0",
                    "rewards": [],
                },
            ]
        }
    }

    rows = parse_markets(payload)
    assert len(rows) == 3
    assert rows[0].version == "v3"
    assert rows[1].displayed_apy_pct == Decimal("6.35")
    assert rows[2].available_liquidity == Decimal("0")


def test_rank_excludes_unavailable_and_paused():
    payload = {
        "data": {
            "markets": [
                {
                    "version": "v3", "chainId": 1, "chain": "Ethereum",
                    "symbol": "USDC", "reserveId": "a",
                    "supplyAPY": "4.0", "availableLiquidity": "1000",
                },
                {
                    "version": "v4", "chainId": 1, "chain": "Ethereum",
                    "symbol": "USDT", "reserveId": "b",
                    "supplyApy": "8.0", "availableLiquidity": "1000",
                    "paused": True,
                },
                {
                    "version": "v4", "chainId": 43114, "chain": "Avalanche",
                    "symbol": "DAI", "reserveId": "c",
                    "supplyApy": "6.0", "availableLiquidity": "5000",
                },
            ]
        }
    }
    ranked = rank_stablecoin_supply(parse_markets(payload))
    assert [r.symbol for r in ranked] == ["DAI", "USDC"]


def test_parse_canonical_aave_reserve_shape():
    payload = {
        "data": {
            "reserves": [
                {
                    "__typename": "Reserve",
                    "market": {
                        "__typename": "MarketInfo",
                        "address": "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2",
                        "chainId": 1,
                    },
                    "underlyingToken": {
                        "__typename": "Currency",
                        "symbol": "USDC",
                        "name": "USD Coin",
                        "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
                    },
                    "reserveId": "canonical-usdc-v3-eth",
                    "summary": {
                        "supplyApy": "3.75",
                        "rewards": [],
                    },
                    "isFrozen": False,
                    "isPaused": False,
                }
            ]
        }
    }
    rows = parse_markets(payload)
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "USDC"
    assert row.chain_id == 1
    assert row.market_address == "0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2"
    assert row.underlying_token_address == "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    assert row.underlying_token_name == "USD Coin"
    assert row.supply_apy_pct == Decimal("3.75")


import pytest


def test_vault_set_fee_validates_aave_minimum(monkeypatch):
    from src.dragon.aave_mcp import AaveMCPClient

    client = AaveMCPClient()

    class NoNetwork:
        async def call(self, *args, **kwargs):
            return args, kwargs

    client.call = NoNetwork().call

    import asyncio
    result = asyncio.run(client.vault_set_fee(
        chain_id=1,
        vault="0x1234567890abcdef1234567890abcdef12345678",
        new_fee_percent=15,
    ))
    assert result[0] == "vaultSetFee"
    assert result[1]["chainId"] == 1
    assert result[1]["newFee"] == "15"


def test_vault_set_fee_rejects_below_ten_percent():
    from src.dragon.aave_mcp import AaveMCPClient
    import asyncio

    with pytest.raises(ValueError, match="between 10% and 100%"):
        asyncio.run(AaveMCPClient().vault_set_fee(
            chain_id=1,
            vault="0x1234567890abcdef1234567890abcdef12345678",
            new_fee_percent=9,
        ))
