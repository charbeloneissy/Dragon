# Dragon Architecture v2

## Scope

Launch scope is exactly three chains:

- Base (Aerodrome is the initial Base venue)
- BNB Chain
- Celo

Aave is the flash-liquidity/protocol intelligence layer where the selected
deployment supports the required asset and operation.

## Hot path

RPC/live state -> market brain -> liquidity brain -> economic brain ->
dynamic sizing/routing -> simulation -> atomic executor -> verifier.

The hot path must not depend on LLM/AI calls, dashboards, analytics queries, or
non-essential database round trips.

## Cold path

Celo AI/Celopedia, protocol discovery, historical calibration, analytics,
configuration and model updates feed normalized facts into the hot path.

AI supplies information. Deterministic Dragon mathematics and the atomic
execution contract control execution.

## Core optimization

For a current state S:

    (Q*, R*, C*) = argmax E[realized_net(Q, R, C, S)]

subject to liquidity, repayment, simulation and execution constraints.

The $0.005 minimum is a hard economic gate; realized execution is verified from
receipts after settlement.
