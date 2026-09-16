import asyncio
import os
import re


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    from src.dragon.dashboard import HTML as DASHBOARD_HTML
    from src.dragon.futures_runner import run as run_futures
    import websockets
    import web_runner

    install(dragon_main)
    dragon_main.DASHBOARD = DASHBOARD_HTML

    # Keep the production stream on the native /ws endpoint. The transport
    # telemetry shim previously rebuilt Config with only a subset of fields,
    # silently dropping capital allocation, safety reserve, expected-profit,
    # and other newer risk controls. The live scanner must receive the exact
    # Config produced by Config.from_env().
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

    # Feed the dashboard with rejection telemetry from the same evaluator used
    # by the live scanner, without changing the economic formula or execution
    # path. A None result means executable depth could not complete the path;
    # a returned result below the configured net-edge floor is an edge reject.
    original_evaluate = dragon_main.evaluate_triangle

    def instrumented_evaluate(*args, **kwargs):
        result = original_evaluate(*args, **kwargs)
        try:
            with dragon_main.LOCK:
                dragon_main.STATE.setdefault("rejection", {})
                dragon_main.STATE.setdefault("rejection_total", 0)
                dragon_main.STATE["rejection_total"] += 1
                if result is None:
                    key = "NO_LIQUIDITY"
                else:
                    from decimal import Decimal
                    net_bps = Decimal(str(result[0]))
                    cfg = dragon_main.Config.from_env()
                    key = "NET_EDGE_REJECTED" if net_bps < Decimal(str(cfg.min_net_edge_bps)) else "NET_EDGE_PASSED"
                dragon_main.STATE["rejection"][key] = dragon_main.STATE["rejection"].get(key, 0) + 1
        except Exception:
            pass
        return result

    dragon_main.evaluate_triangle = instrumented_evaluate

    async def run_futures_resilient():
        # Futures is an optional supervisor. It must never be allowed to bring
        # down the single Dragon web/Spot process when Binance returns 418/429
        # or another transient Futures-side failure.
        while True:
            try:
                await run_futures()
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
        await asyncio.gather(dragon_main.run(), run_futures_resilient())

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
