# Dragon Arbitrage Engine

Phase 1 foundation for a Binance spot arbitrage research and execution system.

## Safety

The engine defaults to DRY_RUN=true. Live execution must be explicitly enabled with `LIVE_TRADING=true` and valid Binance API credentials. Never commit secrets.

## Architecture

- `src/dragon/config.py`: environment configuration and validation
- `src/dragon/models.py`: normalized market and opportunity models
- `src/dragon/arbitrage.py`: opportunity detection and net-edge calculation
- `src/dragon/risk.py`: notional, balance, and execution guards
- `src/dragon/exchange.py`: exchange abstraction
- `src/dragon/engine.py`: orchestration loop
- `tests/`: deterministic unit tests

Phase 1 intentionally does not place live orders. It establishes a tested arbitrage core and a safe execution boundary for the next phase.
