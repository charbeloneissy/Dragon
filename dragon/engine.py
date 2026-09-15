import asyncio


def main():
    """Production entrypoint for the Dragon Binance arbitrage engine."""
    from web_runner import run
    asyncio.run(run())


if __name__ == "__main__":
    main()
