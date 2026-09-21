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
