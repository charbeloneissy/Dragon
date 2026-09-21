# Dragon Aave Agent Guide

Dragon follows Aave's official MCP lifecycle:
1. Discover live markets and supported chains.
2. Inspect the exact reserve, wallet position, risk parameters, and capacity.
3. Simulate the exact action.
4. Build an unsigned execution plan.
5. Wallet-sign externally.
6. Confirm resulting protocol state before dependent actions.

## Hard rules

- Never infer a deployment-specific reserve ID, market selector, Hub, Spoke, or price source from memory. Use the selectors returned by get_markets for the current session.
- A market read without coverage metadata is not evidence about an uncovered chain. Treat chainsNotCovered and chainsNotServed explicitly.
- Never treat an empty result as proof of zero unless the requested chain is covered.
- Aave amount inputs are human/main units, not wei/base units. Rates with a Pct suffix are percentages.
- error warnings stop the workflow. warning must be surfaced. info is contextual.
- Supply, borrow, withdraw, and repay flows require simulation before build. Never build a borrow or withdraw without the corresponding preview.
- A frozen or paused reserve is not universally blocked: action eligibility is operation-specific.
- V4 health factor is position-specific. Never average health factors across positions or chains.
- A supply is not collateral unless collateral is explicitly enabled.
- Prepared transactions are unsigned. Dragon never stores a user's wallet key and never signs on the user's behalf.
- Confirm a submitted transaction from resulting protocol state; a transaction hash alone is not protocol confirmation.
- For yield analysis, skip frozen, paused, and cap-reached reserves and verify capacity against the intended size. Report read coverage and whether the result is a sample or the full covered population.
- sGHO is savings, not a lending collateral position; do not treat its rate as utilization-driven lending APY.
- V3 account-history reads are market/chain scoped; do not present one V3 market's history as a wallet's complete history.

## Dragon-specific execution rule

Aave intelligence and safety refreshes remain off the 500 ms DEX quote path. They may constrain an execution candidate only when the relevant chain, protocol deployment, token, liquidity state, and oracle state are current and known.

## Official source snapshot

- Aave MCP docs: https://aave.com/docs/mcp
- Aave agent docs: https://aave.com/agents
- Aave MCP endpoint: https://mcp.aave.com
- Aave skills: https://github.com/aave/skills
- Skills snapshot: b21a0345f47f5fb8337d6769f927b7b56ff3943a
- Address Book: https://github.com/aave-dao/aave-address-book
- Aave V4: https://github.com/aave/aave-v4
- `src/dragon/aave_101.py` keeps Aave lending semantics (supply/borrow/collateral/liquidation) separate from Dragon's atomic flash-loan liquidity path; reserve flags follow the MCP safety table.
- Flash-loan arbitrage must not be evaluated as a collateralized user position or with lending health-factor assumptions.

- Umbrella staking is an optional yield layer for retained capital. Rewards must be shown separately from Aave supply APY and must not be presented as risk-free yield.
- Umbrella StakeTokens can be slashed for deficits on the protected asset/network. Treat slashing exposure as asset- and network-specific.
- Umbrella withdrawals require cooldown activation followed by the unstake window; never assume immediate liquidity.
- Umbrella deposit/cooldown/redeem/withdraw/claim operations are prepared unsigned only. Do not auto-stake Dragon's flash-loan principal.
