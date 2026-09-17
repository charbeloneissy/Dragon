import asyncio


def main():
    """Production entrypoint for Dragon's 2-leg cross-DEX pipeline."""
    from src.dragon import cross_exchange_runtime

    asyncio.run(cross_exchange_runtime.run())


if __name__ == "__main__":
    main()
