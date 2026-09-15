import asyncio
import os


def main():
    """Production entrypoint for the Dragon Binance arbitrage engine."""
    # Bind the Render web port before any Binance/config/network initialization.
    # This lets Render detect the HTTP listener immediately and provision the
    # public onrender.com route reliably.
    os.environ.setdefault("PORT", "10000")
    from web_runner import start_health_server, run
    start_health_server()
    print(f"DRAGON HTTP | health server listening on 0.0.0.0:{os.environ['PORT']}", flush=True)
    asyncio.run(run())


if __name__ == "__main__":
    main()
