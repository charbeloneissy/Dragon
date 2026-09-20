# Dragon

Dragon is a configuration-driven, multi-chain DEX arbitrage system.

## Core execution principle

CONTROL PLANE -> RISK ENGINE -> EXECUTION ENGINE -> CHAIN

The dashboard is an observer/control interface only. It is not an execution authority.

## Opportunity lifecycle

Observe -> Synchronize State -> Detect -> Predict Survival -> Optimize -> Risk -> Simulate -> Requote -> Targeted Submit -> Confirm -> Prove P&L -> Learn -> Reprioritize

An opportunity is not considered realized until the blockchain receipt and accounting layer establish the actual net P&L.

## Multi-chain

Chains are registry-driven. A chain advertises its RPC providers, native token, DEX venues, gas/finality characteristics and execution capabilities. The core opportunity engine does not need chain-specific branching to add another registered chain.

The initial production target is the seven-chain set in dragon_live_config.yaml: Ethereum, Arbitrum, Base, Optimism, BNB Chain, Polygon and Avalanche.

Other registered chains can remain observe-only until their venue/liquidity/execution adapters are validated.

## RPC intelligence

Dragon maintains provider health information and routes around degraded providers. Quote validity includes freshness and execution-latency budget checks. If critical state disagrees across providers, the opportunity is rejected instead of guessing.

## Profit proof

Expected profit is always net of configured costs:

gross edge - DEX fees - gas - flash-loan fee - slippage - MEV reserve - risk buffer

Realized P&L is calculated separately from actual execution results and is the canonical accounting metric.

## Safety

Current repository configuration keeps live execution disabled. Enabling live execution requires verified executor-contract compatibility, atomic repayment, private/appropriate transaction routing where required, and an independently validated risk/execution path.
