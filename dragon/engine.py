import asyncio
import os


def main():
    """Production entrypoint for the Dragon Binance arbitrage engine."""
    os.environ.setdefault("PORT", "10000")
    from web_runner import start_health_server
    from full_universe_runner import run
    start_health_server()
    print(f"DRAGON HTTP | health server listening on 0.0.0.0:{os.environ['PORT']}", flush=True)
    asyncio.run(run())


if __name__ == "__main__":
    main()
