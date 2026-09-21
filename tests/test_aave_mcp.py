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


def test_vault_set_fee_request_normalizes_transaction_request():
    from src.dragon.aave_mcp import AaveMCPClient
    import asyncio

    client = AaveMCPClient()

    async def fake_vault_set_fee(**kwargs):
        assert kwargs["chain_id"] == 1
        assert kwargs["vault"].startswith("0x")
        assert str(kwargs["new_fee_percent"]) == "15"
        return {
            "vaultSetFee": {
                "to": "0x0000000000000000000000000000000000000001",
                "from": "0x0000000000000000000000000000000000000002",
                "data": "0x1234",
                "value": "0",
                "chainId": 1,
            }
        }

    client.vault_set_fee = fake_vault_set_fee
    result = asyncio.run(client.vault_set_fee_request(
        chain_id=1,
        vault="0x1234567890abcdef1234567890abcdef12345678",
        new_fee_percent=15,
    ))
    assert result == {
        "to": "0x0000000000000000000000000000000000000001",
        "from": "0x0000000000000000000000000000000000000002",
        "data": "0x1234",
        "value": "0",
        "chainId": 1,
    }


def test_v4_supply_eligibility_and_user_suppliable():
    payload = {
        "data": {
            "reserves": [
                {
                    "version": "v4",
                    "market": {"address": "0x1", "chainId": 1},
                    "underlyingToken": {"symbol": "USDC", "name": "USD Coin", "address": "0x2"},
                    "reserveId": "good",
                    "supplyApy": "7.5",
                    "canSupply": True,
                    "canUseAsCollateral": True,
                    "status": {"frozen": False, "paused": False},
                    "userState": {"suppliable": {"amount": {"value": "1000"}}},
                },
                {
                    "version": "v4",
                    "market": {"address": "0x3", "chainId": 1},
                    "underlyingToken": {"symbol": "USDC", "name": "USD Coin", "address": "0x4"},
                    "reserveId": "blocked",
                    "supplyApy": "20.0",
                    "canSupply": False,
                    "canUseAsCollateral": True,
                    "status": {"frozen": False, "paused": False},
                    "userState": {"suppliable": {"amount": {"value": "1000"}}},
                },
            ]
        }
    }
    rows = parse_markets(payload)
    assert rows[0].can_supply is True
    assert rows[0].can_use_as_collateral is True
    assert rows[0].suppliable == Decimal("1000")
    ranked = rank_stablecoin_supply(rows)
    assert [r.reserve_id for r in ranked] == ["good"]


def test_prepare_withdraw_matches_aave_v4_request_shape():
    import asyncio

    from src.dragon.aave_mcp import AaveMCPClient, prepare_withdraw

    client = AaveMCPClient()
    calls = []

    async def fake_preview_action(**kwargs):
        calls.append(("preview", kwargs))
        return {
            "healthFactor": {"current": "2.0", "after": "1.8"},
            "warnings": [],
        }

    async def fake_prepare_action(*, simulation, **kwargs):
        calls.append(("prepare", kwargs))
        assert simulation["_dragon_simulation_ok"] is True
        return {
            "__typename": "TransactionRequest",
            "chainId": 1,
            "to": "0x0000000000000000000000000000000000000001",
        }

    client.preview_action = fake_preview_action
    client.prepare_action = fake_prepare_action

    result = asyncio.run(
        prepare_withdraw(
            client,
            sender="0x0000000000000000000000000000000000000078",
            reserve="SGVsbG8h",
            amount="500",
            withdrawable="1000",
            reserve_paused=False,
        )
    )

    assert result["unsigned"] is True
    assert calls[0][1]["action"]["withdraw"]["amount"]["erc20"]["value"] == {"exact": "500"}
    assert calls[1][1]["action"]["withdraw"]["amount"]["erc20"]["value"] == {"exact": "500"}


def test_prepare_max_withdraw_uses_max_shape():
    import asyncio

    from src.dragon.aave_mcp import AaveMCPClient, prepare_withdraw

    client = AaveMCPClient()

    async def fake_preview_action(**kwargs):
        assert kwargs["action"]["withdraw"]["amount"]["erc20"]["value"] == {"max": True}
        return {"warnings": []}

    async def fake_prepare_action(*, simulation, **kwargs):
        return {"__typename": "TransactionRequest"}

    client.preview_action = fake_preview_action
    client.prepare_action = fake_prepare_action

    result = asyncio.run(
        prepare_withdraw(
            client,
            sender="0x0000000000000000000000000000000000000078",
            reserve="SGVsbG8h",
            maximum=True,
        )
    )
    assert result["unsigned"] is True


def test_prepare_withdraw_rejects_over_withdrawable():
    import asyncio
    import pytest

    from src.dragon.aave_mcp import AaveMCPClient, prepare_withdraw

    with pytest.raises(Exception, match="exceeds live withdrawable"):
        asyncio.run(
            prepare_withdraw(
                AaveMCPClient(),
                sender="0x0000000000000000000000000000000000000078",
                reserve="SGVsbG8h",
                amount="1500",
                withdrawable="1000",
            )
        )
