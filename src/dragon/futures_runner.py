"""Optional Spot/USD-M basis hedge supervisor.

Disabled unless FUTURES_ENABLED=true. Live order placement additionally requires
FUTURES_LIVE_TRADING=true and remains capped at 1x leverage and configured
notional limits.
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal, ROUND_DOWN

from src.dragon.binance import BinanceClient
from src.dragon.futures import BasisHedgeEngine, FuturesClient, FuturesError


def _floor(value, step):
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _spot_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {})
        market = fs.get("MARKET_LOT_SIZE", lot)
        out[s["symbol"]] = {"step": Decimal(str(market.get("stepSize", lot.get("stepSize", "0.000001")))),
                             "min": Decimal(str(market.get("minQty", lot.get("minQty", "0"))))}
    return out


def _futures_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING" or s.get("contractType") != "PERPETUAL":
            continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {})
        out[s["symbol"]] = {"step": Decimal(str(lot.get("stepSize", "0.001"))),
                             "min": Decimal(str(lot.get("minQty", "0"))))}
    return out


async def run():
    enabled = os.getenv("FUTURES_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
    if not enabled:
        await asyncio.Event().wait()
    live = os.getenv("FUTURES_LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"}
    key = os.getenv("BINANCE_API_KEY", "").strip()
    secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not key or not secret:
        raise FuturesError("FUTURES_ENABLED requires Binance credentials")

    spot = BinanceClient(os.getenv("BINANCE_API_BASE", "https://api.binance.com"), key, secret)
    futures = FuturesClient(os.getenv("BINANCE_FUTURES_API_BASE", "https://fapi.binance.com"), key, secret)
    try:
        await asyncio.to_thread(spot.sync_time)
        await asyncio.to_thread(futures.sync_time)
        spot_info = await asyncio.to_thread(spot.exchange_info)
        fut_info = await asyncio.to_thread(futures.exchange_info)
        sf, ff = _spot_filters(spot_info), _futures_filters(fut_info)
        requested = [s.strip().upper() for s in os.getenv("FUTURES_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT").split(",") if s.strip()]
        symbols = [s for s in requested if s in sf and s in ff]
        if not symbols:
            raise FuturesError("no common Spot/USD-M perpetual symbols configured")
        max_notional = Decimal(os.getenv("FUTURES_MAX_NOTIONAL_USDT", "25"))
        min_edge = Decimal(os.getenv("FUTURES_MIN_NET_EDGE_BPS", "8"))
        fee_bps = Decimal(os.getenv("FUTURES_FEE_BPS", "5"))
        funding_buffer = Decimal(os.getenv("FUTURES_FUNDING_BUFFER_BPS", "2"))
        engine = BasisHedgeEngine(futures, spot, max_notional, leverage=1, live=live)
        print(f"DRAGON FUTURES | enabled=True live={live} symbols={','.join(symbols)}", flush=True)

        while True:
            for symbol in symbols:
                try:
                    spot_ticker = await asyncio.to_thread(spot.public, "/api/v3/ticker/bookTicker", {"symbol": symbol})
                    fut_ticker = await asyncio.to_thread(futures.book_ticker, symbol)
                    mark = await asyncio.to_thread(futures.mark_price, symbol)
                    sb = Decimal(str(spot_ticker["bidPrice"]))
                    sa = Decimal(str(spot_ticker["askPrice"]))
                    fb = Decimal(str(fut_ticker["bidPrice"]))
                    funding = Decimal(str(mark.get("lastFundingRate", "0"))) * Decimal("10000")
                    net = (fb / sa - Decimal("1")) * Decimal("10000") - fee_bps * Decimal("2") - max(funding, Decimal("0")) - funding_buffer
                    if net < min_edge:
                        continue
                    positions = await asyncio.to_thread(futures.position_risk, symbol)
                    if any(abs(Decimal(str(p.get("positionAmt", "0")))) > 0 for p in positions):
                        print(f"DRAGON FUTURES | EXISTING_POSITION {symbol}; skip new hedge", flush=True)
                        continue
                    notional = min(max_notional, Decimal(os.getenv("FUTURES_ORDER_NOTIONAL_USDT", str(max_notional))))
                    qty = _floor(notional / sa, max(sf[symbol]["step"], ff[symbol]["step"]))
                    if qty < sf[symbol]["min"] or qty < ff[symbol]["min"]:
                        print(f"DRAGON FUTURES | FILTER_REJECTED {symbol} qty={qty}", flush=True)
                        continue
                    print(f"DRAGON FUTURES | OPPORTUNITY {symbol} net={net:.3f}bps qty={qty} live={live}", flush=True)
                    if live:
                        result = await asyncio.to_thread(engine.open_hedge, symbol, qty, sa, net)
                        print(f"DRAGON FUTURES | HEDGE {symbol} status=FILLED", flush=True)
                except Exception as exc:
                    print(f"DRAGON FUTURES | ERROR {symbol} {exc}", flush=True)
            await asyncio.sleep(float(os.getenv("FUTURES_POLL_SECONDS", "2")))
    finally:
        spot.close()
        futures.close()
