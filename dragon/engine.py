import asyncio
import os
from urllib.parse import urlsplit


def main():
    """Production entrypoint for the Dragon Binance arbitrage engine."""
    os.environ.setdefault("PORT", "10000")

    import web_runner
    from full_universe_runner import run as run_spot
    from src.dragon.futures_runner import run as run_futures

    # Keep one HTTP server and one Dragon process. Normalize URL paths before
    # handing them to the dashboard handler so query strings never turn a valid
    # dashboard route into a 404.
    original_get = web_runner.Handler.do_GET

    def robust_get(self):
        raw_path = self.path
        normalized = urlsplit(raw_path).path or "/"
        if normalized == "/":
            self.path = "/"
        elif normalized in ("/dashboard", "/dashboard/"):
            self.path = normalized
        elif normalized in ("/health", "/healthz"):
            self.path = normalized + ("?" if "?" in raw_path else "")
        try:
            return original_get(self)
        finally:
            self.path = raw_path

    web_runner.Handler.do_GET = robust_get
    web_runner.start_health_server()
    print(
        f"DRAGON HTTP | dashboard listening on 0.0.0.0:{os.environ['PORT']} routes=/,/dashboard,/health,/healthz",
        flush=True,
    )

    async def supervisor():
        # Exactly one Dragon process. Spot, Futures, and dashboard share it.
        await asyncio.gather(run_spot(), run_futures())

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
