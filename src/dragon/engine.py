import asyncio
import re


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    from src.dragon.dashboard import HTML as DASHBOARD_HTML
    from src.dragon.futures_runner import run as run_futures
    from src.dragon.stream_transport import install as install_stream_transport
    import websockets
    import web_runner

    install(dragon_main)
    install_stream_transport(dragon_main)
    dragon_main.DASHBOARD = DASHBOARD_HTML

    # Binance can occasionally stop delivering enough traffic for the default
    # websocket keepalive window. Give the connection more recovery headroom.
    original_connect = websockets.connect

    def resilient_connect(*args, **kwargs):
        kwargs["ping_interval"] = 10
        kwargs["ping_timeout"] = 30
        kwargs["close_timeout"] = 5
        return original_connect(*args, **kwargs)

    websockets.connect = resilient_connect

    # Render routes the public service to the root path. Serve the dashboard
    # directly there instead of relying on a redirect from `/`.
    original_get = dragon_main.Handler.do_GET

    def dashboard_root(self):
        if self.path == "/":
            self.path = "/dashboard"
            try:
                return original_get(self)
            finally:
                self.path = "/"
        return original_get(self)

    dragon_main.Handler.do_GET = dashboard_root

    async def run_futures_resilient():
        # Futures is an optional supervisor. It must never be allowed to bring
        # down the single Dragon web/Spot process when Binance returns 418/429
        # or another transient Futures-side failure.
        while True:
            try:
                await run_futures()
                # A normal return is unexpected for the enabled supervisor, so
                # keep the process alive and retry rather than ending gather().
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                message = str(exc)
                match = re.search(r"backing off\s+([0-9.]+)s", message, re.IGNORECASE)
                wait = float(match.group(1)) if match else 30.0
                wait = max(5.0, wait)
                try:
                    with web_runner.LOCK:
                        web_runner.STATE["futures_errors"] += 1
                        web_runner.STATE["futures_last_error"] = message
                    web_runner.event("FUTURES_SUPERVISOR", f"Futures paused: {message}; retry_in={wait:.0f}s")
                except Exception:
                    pass
                await asyncio.sleep(wait)

    async def supervisor():
        # Keep exactly one Dragon process. Futures failures are isolated so the
        # Spot engine and dashboard remain available while Futures backs off.
        await asyncio.gather(dragon_main.run(), run_futures_resilient())

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
