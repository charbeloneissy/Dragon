import asyncio
import traceback


def main():
    """Start Dragon and keep the web dashboard available on fatal startup errors."""
    from web_runner import start_health_server, STATE, event, run

    # Start the HTTP surface before Binance initialization so Render and the
    # dashboard remain reachable even when an upstream dependency fails.
    start_health_server()
    try:
        asyncio.run(run(start_server=False))
    except Exception as exc:
        STATE["last_error"] = f"FATAL STARTUP ERROR: {exc}"
        event("FATAL", "Dragon startup failed", error=str(exc))
        print("DRAGON FATAL STARTUP ERROR", flush=True)
        traceback.print_exc()
        # Keep the dashboard alive so the actual failure is visible instead
        # of letting the Render process disappear while the port was open.
        try:
            asyncio.run(asyncio.Event().wait())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
