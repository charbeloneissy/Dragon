import asyncio
import traceback


def main():
    """Start Dragon and keep the dashboard reachable if startup fails."""
    from web_runner import STATE, event, run

    try:
        # web_runner.run() starts the HTTP server before Binance initialization.
        asyncio.run(run())
    except Exception as exc:
        STATE["last_error"] = f"FATAL STARTUP ERROR: {exc}"
        event("FATAL", "Dragon startup failed", error=str(exc))
        print("DRAGON FATAL STARTUP ERROR", flush=True)
        traceback.print_exc()
        # Do not let Render lose the process after the dashboard HTTP server
        # has been opened. Keep the server available so the failure is visible.
        try:
            asyncio.run(asyncio.Event().wait())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
