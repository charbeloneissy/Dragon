# Dragon Arbitrage Engine

Production-oriented Binance Spot triangular arbitrage engine.

## Quantitative research architecture

Dragon now separates **measurement** from **execution policy**. The quantitative layer in `src/dragon/research_architecture.py` is pure research code: it places no orders and mutates no balances.

The measurement pipeline is:

```text
RAW MARKET SNAPSHOTS
  -> timestamp / sequence / freshness
  -> synchronized order books
  -> depth and executable VWAP
  -> propagated triangle quantities
  -> gross edge
  -> fees
  -> slippage / market impact
  -> latency penalty
  -> volatility
  -> spread stability / persistence
  -> net expected P&L
  -> empirical P(success)
  -> expected value
  -> opportunity score / tier
  -> hard-gate report
  -> PAPER / REPLAY OUTCOME
  -> REALIZED P&L
```

### Core measurements

- **Executable VWAP:** consumes the required quantity through multiple book levels instead of assuming the best quote is fillable.
- **Quantity propagation:** each triangle leg receives the actual asset amount produced by the previous leg, so the three legs are not incorrectly evaluated at one constant notional.
- **Fees:** measured per leg and included in net output.
- **Slippage and impact:** measured against the best available price for the required quantity.
- **Spread stability:** rolling count, mean, median, min, max, standard deviation and persistence.
- **Volatility:** log-return volatility over 1s, 5s, 15s, 60s, 300s and 900s windows.
- **Latency penalty:** volatility-scaled penalty using `k * sigma_1s * sqrt(latency_seconds)`.
- **Probability:** Bayesian-smoothed empirical probability from completed paper/replay outcomes. It is not guessed from a score.
- **Expected value:** `P(success) * profit - (1-P(success)) * loss`.
- **Score:** 0-100 using net edge, liquidity utilization, persistence and execution-risk penalties. The score cannot override hard gates.
- **Tiers:** A+ 90-100, A 80-89, B 70-79, C 60-69, below 60 = REJECT.

### Research principle

The objective is **repeatable positive expectancy over a sufficiently large sample**, not maximum trade count and not a promise of profit. Thresholds should be calibrated from replay/paper results and then validated on fresh observations.

The 30-second value is a **decision-cycle interval**, not a market-data refresh interval. WebSocket market data remains continuous.

## Runtime architecture

1. Binance REST connector authenticates the account and loads `exchangeInfo`.
2. Triangle builder creates executable USDT -> asset A -> asset B -> USDT cycles.
3. Binance Spot WebSocket streams bookTicker and multi-level depth.
4. The scanner evaluates the same budget that risk management permits, using order-book depth rather than only top-of-book prices.
5. Fees, slippage allowance, stale-book checks, notional limits and the 1% risk ceiling gate execution.
6. The executor places three sequential MARKET legs and refuses to mark the cycle filled unless all three return `FILLED` and the final asset is USDT.
7. The execution ledger records each completed or failed cycle and reports realized P&L for the running service.
8. `/health` exposes machine-readable state and `/dashboard` exposes the live monitoring page.

## Environment

Secrets must only be configured in Render environment variables. Never commit API keys or secrets.

Required live variables:

- `BINANCE_API_KEY`
- `BINANCE_API_SECRET`
- `DRY_RUN=false`
- `LIVE_TRADING=true`

The complete non-secret configuration is in `.env.example`.

## Live-trading safety

Dragon will not start live execution when credentials are missing, when `LIVE_TRADING` conflicts with `DRY_RUN`, when risk configuration is invalid, or when an execution cannot complete a full three-leg cycle.

A Binance API error `-2015` is an exchange-side credential, permission, or IP restriction failure. Code cannot manufacture a valid key. The Render secret must be a valid Binance Spot trading key with the required permissions and compatible IP restrictions.

## Important limitation

Triangular Spot arbitrage is sequential. There is no atomic three-leg order on Binance Spot, so market movement between legs remains execution risk. Dragon therefore sizes from current free USDT, uses depth-aware estimates, enforces strict stale-book and risk gates, and records every completed/error cycle.
