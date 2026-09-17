import asyncio


def main():
    """Production entrypoint for Dragon's 2-leg cross-DEX pipeline."""
    from src.dragon import dex_cross_exchange

    asyncio.run(dex_cross_exchange.run())


if __name__ == "__main__":
    main()
