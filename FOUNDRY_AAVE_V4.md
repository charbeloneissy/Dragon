# Dragon Foundry / Aave V4

Install the official Aave V4 dependency before compiling the Solidity lens:

```bash
forge install aave/aave-v4
```

Then:

```bash
forge build
forge test
```

The Dragon Aave V4 lens is read-only. It exposes Hub asset liquidity, per-Spoke credit-line/cap state, and Spoke Reserve configuration/liquidity. It does not sign or broadcast transactions.
