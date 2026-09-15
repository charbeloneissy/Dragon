# Dragon Arbitrage Engine

Production-oriented Binance Spot triangular arbitrage engine.

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
