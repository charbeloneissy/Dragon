# Dragon — Multi-Chain Cross-DEX Arbitrage Engine

Dragon scans **cross-exchange arbitrage between DEX venues** on EVM and non-EVM
chains: it prices the same token pair on every supported venue, finds the venue
where a token is cheap and the venue where it is expensive, and reports the
round-trip edge after swap fees, gas, flash-loan fees and a safety buffer.

There is no centralized-exchange (CEX) code left in this repository. Every quote
is an executable on-chain quote.

## Supported chains

One EVM adapter serves every EVM chain through a shared registry
(`src/dragon/chains.py`, `src/dragon/venues.py`): adding a chain or venue is a
registry edit, not new code. Verified live venues:

| Chain | Id | Venues |
| --- | --- | --- |
| Ethereum | 1 | Uniswap_V3, SushiSwap_V2, Uniswap_V2, PancakeSwap_V3 |
| Optimism | 10 | Uniswap_V3, Velodrome_V2 |
| BNB Chain | 56 | Uniswap_V3, PancakeSwap_V3, Uniswap_V2, PancakeSwap_V2, BiSwap_V2 |
| Unichain | 130 | Uniswap_V3 |
| Polygon | 137 | Uniswap_V3, QuickSwap_V2 |
| zkSync | 324 | Uniswap_V3 |
| Worldchain | 480 | Uniswap_V3 |
| Mantle | 5000 | MerchantMoe_V2 |
| Base | 8453 | Uniswap_V3, Aerodrome, PancakeSwap_V3, SushiSwap_V3, SushiSwap_V2, Uniswap_V2, BaseSwap_V2, SwapBased_V2 |
| Arbitrum | 42161 | Uniswap_V3, Camelot_V2 |
| Celo | 42220 | Uniswap_V3 |
| Avalanche | 43114 | Uniswap_V3, SushiSwap_V2, TraderJoe_V2_1 |
| Linea | 59144 | Uniswap_V3 |

Non-EVM venues (`src/dragon/dex_nonevm.py`):

| Family | Venue | Status |
| --- | --- | --- |
| Tron | SunSwap_V2 | verified live |
| Cosmos | Osmosis (SQS router) | verified live |
| Cosmos | Astroport | wired, disabled until `ASTROPORT_*_ROUTER` is set |
| Aptos | Liquidswap | wired, config-gated |

Notes:

- **Solana** uses the Jupiter quote API when `JUPITER_API_KEY` is configured.
- **Ravencoin (RVN)** is a Bitcoin fork with no EVM and no on-chain AMM/DEX, so
  there is nothing to arbitrage. It is deliberately absent from the registry.

## How it works

```text
chains.py / venues.py       registry: chain ids, RPC env, token addresses, venue routers
        |
dex_evm.py                  RpcPool (failover, cooldown) + EVM quoting:
                            Uniswap V2, Uniswap V3 (QuoterV2), stable swaps, LB
dex_nonevm.py               Tron (SunSwap), Cosmos (Osmosis/Astroport), Aptos (Liquidswap)
        |
dex_multichain.py           MultiChainDexAdapter facade: one quote surface for all chains
        |
dex_cross_exchange.py       DexCrossExchangeEngine: venue-pair scan, cost model,
                            dynamic amount optimizer, rejection accounting
        |
dex_cross_exchange_runner.py  production entrypoint: HTTP health + dashboard, scan loop
```

For each base token the engine quotes a buy leg on every venue and a sell leg on
every other venue, then computes:

```text
gross_profit = sell_output - quote_invested
net_profit   = gross_profit - swap_fees - gas_cost - flash_loan_fee - safety_buffer
```

Only net-positive routes above `DEX_MIN_NET_PROFIT` are reported.
`GET /health` returns full machine state; `GET /dashboard` renders `dashboard.html`.

## Configuration

Copy `.env.example` to `.env`. Key settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEX_CHAINS` | `8453` | Comma-separated EVM chain ids to scan |
| `NONEVM_CHAINS` | *(empty)* | Non-EVM families: `tron,cosmos,aptos,solana` |
| `JUPITER_API_KEY` | *(empty)* | Bearer key for Solana Jupiter quotes |
| `ALCHEMY_API_KEY` | *(empty)* | Alchemy key; covers eth/arb/opt/polygon/base/avax/bnb/linea/zksync/scroll/blast |
| `INFURA_API_KEY` | *(empty)* | Infura project id |
| `QUICKNODE_API_KEY` + `QUICKNODE_ENDPOINT` + `QUICKNODE_CHAIN_ID` | *(empty)* | QuickNode endpoint for one chain |
| `<CHAIN>_RPC_URL(S)` | public fallback | Per-chain RPC endpoints (always win over providers) |
| `DEX_SOURCES` | all venues | Restrict venues per chain |
| `DEX_QUOTE_TOKENS` | per-chain stablecoin | `"8453:0x...:6"` overrides |
| `DEX_MIN_NET_PROFIT` | `0.005` | Minimum net profit in quote units (floor) |
| `DEX_FLASH_LOAN_LIQUIDITY_QUOTE` | `1000` | Flash-loan principal cap (quote units) |
| `DEX_POLL_SECONDS` | `2.0` | Seconds between full scans |
| `FLASH_LOAN_ENABLED` | `false` | Compute flash-loan fees into net profit |

Without a custom RPC the public endpoints work but are rate-limited; a private
RPC per chain is strongly recommended for production throughput.

## Run and test

```bash
pip install -r requirements.txt
cp .env.example .env
python dex_cross_exchange_runner.py     # serves /health and /dashboard
pytest -q                               # 33 tests
```

Paper mode is the default. Live execution stays disabled until
`LIVE_TRADING=true` plus a deployed atomic executor are configured.
