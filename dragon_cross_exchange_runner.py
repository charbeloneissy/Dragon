from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.cross_exchange_futures import CrossExchangeFutures

STATE = {
    "status": "starting",
    "mode": "paper",
    "exchanges": 0,
    "configured_exchanges": 0,
    "scans": 0,
    "opportunities": 0,
    "executions": 0,
    "open_positions": 0,
    "execution_halted": False,
    "last_execution": None,
    "last_scan": None,
    "last_error": None,
    "started_at": time.time(),
}
LOCK = Lock()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            with LOCK:
                payload = dict(STATE)
            body = json.dumps(payload, default=str).encode()
            self.send_response(200 if payload["status"] in {"starting", "running", "degraded"} else 503)
            self.send_header("Content-Type", "application/json")
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
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Dragon health server listening on 0.0.0.0:%s", port)


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    start_health_server()
    bot = CrossExchangeFutures()

    with LOCK:
        STATE["mode"] = "live" if bot.settings.live else "paper"
        STATE["configured_exchanges"] = len(bot.settings.exchanges)

    logging.info(
        "Dragon cross-exchange futures | mode=%s | configured_exchanges=%s | starting_balance=%s | min_profit=%s",
        STATE["mode"],
        len(bot.settings.exchanges),
        bot.settings.starting_balance,
        bot.settings.min_profit_usdt,
    )

    try:
        await bot.load()
        with LOCK:
            STATE["exchanges"] = len(bot.exchanges)
            STATE["status"] = "running"

        logging.info(
            "Dragon loaded %s/%s futures exchanges",
            len(bot.exchanges),
            len(bot.settings.exchanges),
        )

        if len(bot.exchanges) != len(bot.settings.exchanges):
            raise RuntimeError(
                f"only {len(bot.exchanges)}/{len(bot.settings.exchanges)} configured exchanges loaded"
            )

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
                    logging.info(
                        "OPPORTUNITY symbol=%s long=%s short=%s qty=%s net_profit=%s",
                        best.symbol,
                        best.long_exchange,
                        best.short_exchange,
                        best.quantity,
                        best.net_profit,
                    )
                    result = await bot.execute(best)
                    with LOCK:
                        STATE["executions"] += 1
                        STATE["last_execution"] = result.get("status")
                        STATE["execution_halted"] = bot.execution_halted
                    logging.info(
                        "EXECUTION status=%s reason=%s",
                        result.get("status"),
                        result.get("reason", ""),
                    )

                # Position management is part of the execution lifecycle. Without
                # this call, successfully opened hedges could remain open forever.
                await bot.manage_positions()
                with LOCK:
                    STATE["open_positions"] = len(bot.positions)
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
