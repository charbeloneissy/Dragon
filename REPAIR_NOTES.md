# Repair status

The Dragon execution path is configured for hybrid flash-loan plus retained-profit compounding: own external capital remains zero, cross-DEX only, triangular arbitrage is not used, minimum net profit is $0.005, and the flash-loan ceiling is 1,000 quote units. Live mode uses the lower of the configured 1,000-unit cap and actual Aave pool liquidity, plus the on-chain Aave premium. The executor can retain realized quote-token profit, then add a bounded portion of that reserve to the next flash-loan trade.

Live trading remains disabled until the deployed executor contract and private MEV RPC are verified.

## Hybrid compounding

`DEX_COMPOUND_PROFITS=true` keeps the executor's realized profit in its quote-token reserve instead of transferring it to the owner. `DEX_COMPOUND_RATIO` selects the fraction of that reserve used on the next trade, and `DEX_MAX_COMPOUND_QUOTE` caps the amount. The Aave principal is still borrowed and repaid atomically on every trade; compounding never removes the flash-loan repayment requirement. A reverted transaction returns the retained reserve through EVM atomicity, but the owner wallet still needs gas.

The `FlashParams` ABI now includes `compoundAmount` and `compoundProfit`; the executor contract must be redeployed and its new address configured before any live call. An old deployment is not compatible with the hybrid calldata.

## Execution observability

The DEX runner now exposes `observability.counters` and bounded
`observability.recent_records` from `/health`. Records carry one opportunity ID
through quote gates, explicit `eth_call`, submission, inclusion or reversion,
and the executor's `FlashArbitrageExecuted` profit event. The dashboard renders
the same lifecycle so estimated opportunity profit is not confused with
realized P&L.

The active runner uses direct executable quotes, not a pool-event stream;
`pool_events_received` therefore remains zero unless that data source is wired
into the runner. `quote_observations` and `pools_with_fresh_state` show the
observability that is actually active.

The Render source list is intentionally limited to `Uniswap_V3,Aerodrome` until
the optional 0x API returns authenticated, non-rate-limited quotes. A 0x HTTP
403 or repeated slow response must not prevent the direct DEX sources from
ranking opportunities.
