import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit


async def run_production():
    """Run exactly one Dragon Spot full-universe engine plus its dashboard."""
    os.environ.setdefault("PORT", "10000")

    import web_runner
    from full_universe_runner import full_universe_stream_loop
    from src.dragon.binance import BinanceClient
    from src.dragon.config import Config
    from src.dragon.ledger import Ledger
    from src.dragon.triangles import build_triangles

    original_get = web_runner.Handler.do_GET
    dashboard_path = Path(__file__).resolve().parent.parent / "dashboard.html"

    def robust_get(self):
        """Serve the dashboard reliably, including query strings such as ?utm_source=... ."""
        raw_path = self.path or "/"
        normalized = urlsplit(raw_path).path or "/"

        if normalized in ("/dashboard", "/dashboard/"):
            # Prefer the repository dashboard, but fall back to the embedded
            # dashboard so a missing/mis-mounted dashboard.html can never cause 404.
            if dashboard_path.is_file():
                body = dashboard_path.read_bytes()
            else:
                body = web_runner.DASHBOARD.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # web_runner historically matched only exact paths. Normalize the path
        # before delegating so /health?ts=... and similar requests remain valid.
        self.path = normalized
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

    cfg = Config.from_env()
    cfg.validate()

    with web_runner.LOCK:
        web_runner.STATE["started_at"] = __import__("time").time()
        web_runner.STATE["live"] = cfg.live_trading
        web_runner.STATE["dry_run"] = cfg.dry_run
        web_runner.STATE["status"] = "starting"

    web_runner.LEDGER = Ledger()
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    client = BinanceClient(cfg.api_base, api_key, api_secret)

    try:
        if cfg.live_trading and not cfg.dry_run:
            if not api_key or not api_secret:
                raise RuntimeError("LIVE_TRADING requires BINANCE_API_KEY and BINANCE_API_SECRET")
            account = client.account()
            with web_runner.LOCK:
                web_runner.STATE["binance_authenticated"] = True
            free = next(
                (x.get("free", "0") for x in account.get("balances", []) if x.get("asset") == "USDT"),
                "0",
            )
            with web_runner.LOCK:
                web_runner.STATE["free_usdt"] = str(free)
            web_runner.event("AUTH", "Binance API authenticated successfully", usdt_free=str(free))
        else:
            web_runner.event("SAFE", "Live execution disabled")

        info = client.exchange_info()
        filters = web_runner.make_filters(info)
        symbol_meta = web_runner._symbol_meta(info)
        triangles = build_triangles(info, cfg.max_triangles)
        symbols = sorted({s for triangle in triangles for s in triangle.symbols})

        with web_runner.LOCK:
            web_runner.STATE["triangles"] = len(triangles)
            web_runner.STATE["symbols"] = len(symbols)
            web_runner.STATE["market_streams"] = len(symbols)

        web_runner.event(
            "UNIVERSE",
            f"FULL SPOT UNIVERSE active: triangles={len(triangles)} symbols={len(symbols)}",
        )
        web_runner.event(
            "START",
            f"Dragon full-universe engine ready; live={cfg.live_trading and not cfg.dry_run}",
        )

        await full_universe_stream_loop(
            cfg,
            client,
            filters,
            triangles,
            symbols,
            symbol_meta,
        )
    finally:
        client.close()


def main():
    asyncio.run(run_production())


if __name__ == "__main__":
    main()
