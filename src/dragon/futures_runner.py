"""USDⓈ-M perpetual basis supervisor with full Futures-universe discovery."""
from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal, ROUND_DOWN

from src.dragon.binance import BinanceClient
from src.dragon.control import analysis_allowed, trading_allowed
from src.dragon.futures import BasisHedgeEngine, FuturesClient, FuturesError, evaluate_basis
import web_runner


def _floor(value, step):
    if step <= 0: return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _spot_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT": continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}; lot = fs.get("LOT_SIZE", {}); market = fs.get("MARKET_LOT_SIZE", lot)
        out[s["symbol"]] = {"step": Decimal(str(market.get("stepSize", lot.get("stepSize", "0.000001")))), "min": Decimal(str(market.get("minQty", lot.get("minQty", "0"))))}
    return out


def _futures_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL" or s.get("quoteAsset") != "USDT" or s.get("marginAsset") != "USDT": continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}; lot = fs.get("LOT_SIZE", {})
        out[s["symbol"]] = {"step": Decimal(str(lot.get("stepSize", "0.001"))), "min": Decimal(str(lot.get("minQty", "0")))}
    return out


def _requested_universe(ff):
    configured = os.getenv("FUTURES_SYMBOLS", "").strip(); universe = sorted(ff)
    if configured:
        requested = {s.strip().upper() for s in configured.split(",") if s.strip()}; universe = [s for s in universe if s in requested]
    return universe


async def run():
    enabled = os.getenv("FUTURES_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    if not enabled: await asyncio.Event().wait()
    live = os.getenv("FUTURES_LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"}
    key = os.getenv("BINANCE_API_KEY", "").strip(); secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not key or not secret: raise FuturesError("FUTURES_ENABLED requires Binance credentials")
    spot = BinanceClient(os.getenv("BINANCE_API_BASE", "https://api.binance.com"), key, secret)
    futures = FuturesClient(os.getenv("BINANCE_FUTURES_API_BASE", "https://fapi.binance.com"), key, secret)
    try:
        await asyncio.to_thread(spot.sync_time)
        spot_info = await asyncio.to_thread(spot.exchange_info)
        fut_info = await asyncio.to_thread(futures.exchange_info)
        sf, ff = _spot_filters(spot_info), _futures_filters(fut_info); symbols = _requested_universe(ff)
        if not symbols: raise FuturesError("no tradable USDT-margined perpetual symbols configured")
        max_symbols = int(os.getenv("FUTURES_MAX_SYMBOLS", "0"))
        if max_symbols > 0: symbols = symbols[:max_symbols]
        max_notional = Decimal(os.getenv("FUTURES_MAX_NOTIONAL_USDT", "25"))
        min_edge = Decimal(os.getenv("FUTURES_MIN_NET_EDGE_BPS", "12"))
        fee_bps = Decimal(os.getenv("FUTURES_FEE_BPS", "5"))
        funding_buffer = Decimal(os.getenv("FUTURES_FUNDING_BUFFER_BPS", "3"))
        slippage_bps = Decimal(os.getenv("FUTURES_SLIPPAGE_BPS", "1"))
        poll_seconds = max(30.0, float(os.getenv("FUTURES_POLL_SECONDS", "30")))
        engine = BasisHedgeEngine(futures, spot, max_notional, leverage=1, live=live)
        with web_runner.LOCK:
            web_runner.STATE["futures_enabled"] = True; web_runner.STATE["futures_live"] = live; web_runner.STATE["futures_universe"] = len(symbols)
        web_runner.event("FUTURES", f"USDⓈ-M validator ready: universe={len(symbols)} live={live} poll={poll_seconds:.0f}s bidirectional=true paper_validation=true")
        while True:
            if not analysis_allowed() or not trading_allowed("futures"):
                await asyncio.sleep(2); continue
            try:
                spot_rows = await asyncio.to_thread(spot.public, "/api/v3/ticker/bookTicker")
                fut_rows = await asyncio.to_thread(futures.all_book_tickers)
                marks = await asyncio.to_thread(futures.all_mark_prices)
                spot_by = {row.get("symbol"): row for row in spot_rows if row.get("symbol") in sf}
                fut_by = {row.get("symbol"): row for row in fut_rows if row.get("symbol") in ff}
                mark_by = {row.get("symbol"): row for row in marks if row.get("symbol") in ff}
                rows = []
                for symbol in symbols:
                    ft = fut_by.get(symbol); st = spot_by.get(symbol); mark = mark_by.get(symbol)
                    if not ft or not st or not mark: continue
                    try:
                        funding = Decimal(str(mark.get("lastFundingRate", "0"))) * Decimal("10000")
                        rows.append({
                            "symbol": symbol,
                            "spotBid": st["bidPrice"], "spotAsk": st["askPrice"],
                            "futuresBid": ft["bidPrice"], "futuresAsk": ft["askPrice"],
                            "fundingBps": str(funding),
                        })
                    except (KeyError, ValueError, ArithmeticError):
                        continue
                opportunities = evaluate_basis(rows, fee_bps, min_edge, slippage_bps)
                for symbol, edge, direction in opportunities[:50]:
                    row = next((r for r in rows if r["symbol"] == symbol), None)
                    if not row: continue
                    web_runner.event("FUTURES_OPPORTUNITY", f"{symbol} direction={direction} net={edge:.3f}bps funding={Decimal(str(row['fundingBps'])):.3f}bps live={live} execution=SIMULATED")
                    # Futures validation is paper-only. Do not submit leveraged
                    # orders from this validator. Keep the existing execution
                    # primitive isolated for separately reviewed production use.
                    if not live:
                        continue
                    web_runner.event("FUTURES_BLOCKED", f"{symbol} live execution intentionally disabled by validator; positive edge={edge:.3f}bps direction={direction}")
                with web_runner.LOCK:
                    web_runner.STATE["futures_scans"] += 1
                    web_runner.STATE["futures_opportunities"] += len(opportunities)
                    web_runner.STATE["futures_last_scan"] = time.time()
                web_runner.event("FUTURES_SCAN", f"complete universe={len(symbols)} opportunities={len(opportunities)} best={(opportunities[0] if opportunities else None)}")
            except FuturesError as exc:
                with web_runner.LOCK: web_runner.STATE["futures_errors"] += 1; web_runner.STATE["futures_last_error"] = str(exc)
                web_runner.event("FUTURES_BACKOFF", str(exc))
            except Exception as exc:
                with web_runner.LOCK: web_runner.STATE["futures_errors"] += 1; web_runner.STATE["futures_last_error"] = str(exc)
                web_runner.event("FUTURES_MARKET_ERROR", str(exc))
            await asyncio.sleep(poll_seconds)
    finally:
        spot.close(); futures.close()
