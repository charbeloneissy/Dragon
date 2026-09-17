# Repair status

The Dragon execution path is configured for flash-loan-only operation: own capital is zero, cross-DEX only, triangular arbitrage is not used, minimum net profit is $0.005, and the flash-loan ceiling is $100 in paper mode. Live mode uses the actual Aave pool liquidity and the on-chain Aave premium.

Live trading remains disabled until the deployed executor contract and private MEV RPC are verified.
