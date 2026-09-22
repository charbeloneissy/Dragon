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
| Scroll | 534352 | SyncSwap_V2, Uniswap_V3, SushiSwap_V2 |

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

### Requested universe

The screenshot-derived universe contains 48 selected chains and native assets.
It is exposed in `/health` as `universe` and in the dashboard as active versus
watchlist counts. `DEX_CHAINS` and `NONEVM_CHAINS` still control executable
quote scans; chains without a verified adapter remain visible as watchlist
entries until their RPC, token map, and at least two EVM DEX venues are verified.

## Configuration

Copy `.env.example` to `.env`. Key settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEX_CHAINS` | `8453` | Comma-separated EVM chain ids to scan |
| `UNIVERSE_CHAINS` | *(all)* | Filter the 48 requested chain/native-coin watchlist |
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
| `DEX_POLL_SECONDS` | `30` | Seconds between full opportunity-search cycles |
| `FLASH_LOAN_ENABLED` | `false` | Compute flash-loan fees into net profit |
| `AERODROME_POOLS_JSON` | empty | Optional live/externally refreshed Aerodrome pool snapshots for read-only capital ranking |
| `AERODROME_MIN_TVL_USD` | `1000` | Minimum Aerodrome TVL considered by the allocator |
| `AERODROME_ARB_INTERACTION_WEIGHT` | `0.20` | Weight of Dragon arbitrage interaction in pool score |
| `AERODROME_LP_RETURN_WEIGHT` | `0.55` | Weight of net LP economics in pool score |
| `AERODROME_VOTE_RETURN_WEIGHT` | `0.25` | Weight of voting/incentive economics in pool score |

Without a custom RPC the public endpoints work but are rate-limited; a private
RPC per chain is strongly recommended for production throughput.

### Aerodrome capital allocator

Dragon now includes a **read-only Aerodrome Opportunity Engine**. It evaluates
pool TVL, trading fees, AERO rewards, voting incentives, estimated impermanent
loss, gas and Dragon arbitrage interaction. It produces a capital score but
**cannot deposit, stake, vote or move funds**.

The optional `AERODROME_POOLS_JSON` input is an array of pool snapshots using
the fields in `src/dragon/aerodrome_opportunity.py`. This keeps the hot
arbitrage quote path independent from slower Aerodrome economics. The intended
next data source is a live Aerodrome pool/gauge adapter; until that is wired,
no capital-allocation action is automatic.

## Run and test

```bash
pip install -r requirements.txt
cp .env.example .env
python dex_cross_exchange_runner.py     # serves /health and /dashboard
pytest -q                               # 33 tests
```

Paper mode is the default. Live execution stays disabled until
`LIVE_TRADING=true` plus a deployed atomic executor are configured.
