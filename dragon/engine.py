import asyncio
import os


def main():
    """Production entrypoint for the Dragon Binance arbitrage engine."""
    os.environ.setdefault("PORT", "10000")
    from web_runner import start_health_server
    from full_universe_runner import run as run_spot
    from src.dragon.futures_runner import run as run_futures

    start_health_server()
    print(f"DRAGON HTTP | health server listening on 0.0.0.0:{os.environ['PORT']}", flush=True)

    async def supervisor():
        await asyncio.gather(run_spot(), run_futures())

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
