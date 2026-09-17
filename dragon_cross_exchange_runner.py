from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.cross_exchange_futures import CrossExchangeFutures

STATE = {"status":"starting","mode":"paper","exchanges":0,"configured_exchanges":0,"scans":0,"opportunities":0,"executions":0,"open_positions":0,"execution_halted":False,"last_execution":None,"last_scan":None,"last_error":None,"started_at":time.time()}
LOCK = Lock()


def dashboard_page(payload: dict) -> bytes:
    p = html.escape(json.dumps(payload, default=str))
    status = html.escape(str(payload.get("status", "unknown")))
    mode = html.escape(str(payload.get("mode", "unknown")))
    error = html.escape(str(payload.get("last_error") or "None"))
    return f'''<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="5"><title>Dragon Dashboard</title><style>body{{font-family:system-ui;background:#0b0f14;color:#eef2f6;margin:0;padding:20px}}.wrap{{max-width:1050px;margin:auto}}h1{{margin:0 0 6px}}.sub{{color:#9aa6b2;margin-bottom:22px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card{{background:#141a22;border:1px solid #283241;border-radius:14px;padding:16px}}.label{{color:#8f9baa;font-size:12px;text-transform:uppercase;letter-spacing:.08em}}.value{{font-size:25px;font-weight:700;margin-top:8px}}.ok{{color:#72d6a0}}.warn{{color:#f2c66d}}.bad{{color:#f27b7b}}pre{{white-space:pre-wrap;background:#10151c;border:1px solid #283241;border-radius:14px;padding:16px;overflow:auto}}small{{color:#8290a0}}</style></head><body><div class="wrap"><h1>🐉 Dragon Dashboard</h1><div class="sub">2-leg cross-exchange monitor · auto-refresh 5s</div><div class="grid"><div class="card"><div class="label">Status</div><div class="value">{status}</div></div><div class="card"><div class="label">Mode</div><div class="value">{mode}</div></div><div class="card"><div class="label">Exchanges</div><div class="value">{payload.get('exchanges')}/{payload.get('configured_exchanges')}</div></div><div class="card"><div class="label">Scans</div><div class="value">{payload.get('scans')}</div></div><div class="card"><div class="label">Opportunities</div><div class="value">{payload.get('opportunities')}</div></div><div class="card"><div class="label">Executions</div><div class="value">{payload.get('executions')}</div></div><div class="card"><div class="label">Open positions</div><div class="value">{payload.get('open_positions')}</div></div><div class="card"><div class="label">Execution halted</div><div class="value">{payload.get('execution_halted')}</div></div></div><div class="card" style="margin-top:12px"><div class="label">Last error</div><div class="value" style="font-size:16px">{error}</div></div><div class="card" style="margin-top:12px"><div class="label">Raw telemetry</div><pre>{p}</pre></div><small>Paper/live state is read from the running Dragon process. Dashboard is monitoring only; it does not enable trading.</small></div></body></html>'''.encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            with LOCK:
                payload = dict(STATE)
            body = json.dumps(payload, default=str).encode()
            self.send_response(200 if payload["status"] in {"starting","running","degraded"} else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/dashboard":
            with LOCK:
                payload = dict(STATE)
            body = dashboard_page(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args):
        return


def start_health_server() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Dragon health/dashboard server listening on 0.0.0.0:%s", os.getenv("PORT", "10000"))


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    start_health_server()
    bot = CrossExchangeFutures()
    with LOCK:
        STATE["mode"] = "live" if bot.settings.live else "paper"
        STATE["configured_exchanges"] = len(bot.settings.exchanges)
    logging.info("Dragon cross-exchange futures | mode=%s | configured_exchanges=%s | starting_balance=%s | min_profit=%s", STATE["mode"], len(bot.settings.exchanges), bot.settings.starting_balance, bot.settings.min_profit_usdt)
    try:
        await bot.load()
        with LOCK:
            STATE["exchanges"] = len(bot.exchanges)
            STATE["status"] = "running"
        logging.info("Dragon loaded %s/%s futures exchanges", len(bot.exchanges), len(bot.settings.exchanges))
        if len(bot.exchanges) != len(bot.settings.exchanges):
            raise RuntimeError(f"only {len(bot.exchanges)}/{len(bot.settings.exchanges)} configured exchanges loaded")
        if bot.settings.live and not await bot.reconcile_startup_positions():
            raise RuntimeError("startup position reconciliation failed; execution halted")
        while True:
            try:
                opportunities = await bot.scan_once()
                with LOCK:
                    STATE["scans"] += 1
                    STATE["opportunities"] += len(opportunities)
                    STATE["last_scan"] = time.time()
                    STATE["last_error"] = None
                    STATE["execution_halted"] = bot.execution_halted
                if opportunities and not bot.execution_halted:
                    best = opportunities[0]
                    logging.info("OPPORTUNITY symbol=%s long=%s short=%s qty=%s net_profit=%s", best.symbol, best.long_exchange, best.short_exchange, best.quantity, best.net_profit)
                    result = await bot.execute(best)
                    with LOCK:
                        STATE["executions"] += 1
                        STATE["last_execution"] = result.get("status")
                        STATE["execution_halted"] = bot.execution_halted
                    logging.info("EXECUTION status=%s reason=%s", result.get("status"), result.get("reason", ""))
                manager = getattr(bot, "manage_positions", None)
                if callable(manager):
                    await manager()
                with LOCK:
                    STATE["open_positions"] = len(getattr(bot, "positions", []))
                    STATE["execution_halted"] = bot.execution_halted
                await asyncio.sleep(bot.settings.poll_ms / 1000)
            except Exception as exc:
                logging.exception("scan loop failed")
                with LOCK:
                    STATE["status"] = "degraded"
                    STATE["last_error"] = str(exc)
                    STATE["execution_halted"] = bot.execution_halted
                await asyncio.sleep(2)
                with LOCK:
                    STATE["status"] = "running"
    finally:
        for exchange in bot.exchanges.values():
            try:
                await exchange.close()
            except Exception:
                logging.exception("exchange close failed")


if __name__ == "__main__":
    asyncio.run(main())
