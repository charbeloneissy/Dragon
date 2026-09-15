import asyncio
import traceback


def main():
    """Start the HTTP dashboard before initializing the trading engine."""
    import web_runner

    # Bind the dashboard first. This prevents a configuration/Binance startup
    # failure from making Render's web service appear unavailable.
    dashboard_server = web_runner.start_health_server()

    # web_runner.run() also calls start_health_server(). Make that call
    # idempotent for this process so we do not bind the Render port twice.
    web_runner.start_health_server = lambda: dashboard_server

    try:
        asyncio.run(web_runner.run())
    except Exception as exc:
        web_runner.STATE["last_error"] = f"FATAL STARTUP ERROR: {exc}"
        web_runner.event("FATAL", "Dragon startup failed", error=str(exc))
        print("DRAGON FATAL STARTUP ERROR", flush=True)
        traceback.print_exc()
        # Keep the dashboard available so the failure remains observable.
        try:
            asyncio.run(asyncio.Event().wait())
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
