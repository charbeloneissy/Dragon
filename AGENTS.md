# Dragon Aave Agent Guide

Dragon uses Aave's official agent pattern:
1. Discover live markets and supported chains.
2. Inspect the exact reserve / position / liquidity state.
3. Simulate the exact action.
4. Build an unsigned execution plan.
5. Hand the transaction to the external wallet for signing.
6. Confirm the post-transaction state from chain data.

## Hard rules

- Never infer a deployment-specific reserve ID, market selector, Hub, Spoke, or price source from memory.
- Never treat an empty result as proof of zero unless the response says the requested chain was covered.
- `error` warnings stop the workflow. `warning` must be surfaced. `info` is contextual.
- Supply / borrow / withdraw / repay flows require simulation before build.
- A frozen reserve is not universally blocked: withdrawal and repayment may remain valid; supply/borrow rules differ.
- V4 Health Factor is position-specific. Do not average health factors across positions or chains.
- A prepared transaction is unsigned. Dragon never stores the user's wallet key and never signs on the user's behalf.
- Confirm a transaction using resulting protocol state, not only the transaction hash.

## Dragon-specific execution rule

Aave intelligence and safety refreshes run off the 500 ms DEX quote path. They can constrain an execution candidate only when the relevant chain, protocol deployment, token, liquidity state, and oracle state are current and known.

Official references:
- https://aave.com/agents
- https://mcp.aave.com
- https://aave.com/docs/mcp/safety
- https://github.com/aave/skills
- https://github.com/aave-dao/aave-address-book
- https://github.com/aave/aave-v4