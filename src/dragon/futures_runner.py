"""USDⓈ-M perpetual basis supervisor with dynamic universe discovery.

The scanner covers all currently tradable USDT-settled perpetuals that also have
an eligible Spot counterpart. It uses bulk market-data endpoints so universe
size does not multiply REST request rate. Live order writes remain capped and
are never blindly retried.
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
        if s.get("status") != "TRADING" or s.get("quoteAsset") != "USDT":
            continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {})
        market = fs.get("MARKET_LOT_SIZE", lot)
        out[s["symbol"]] = {
            "step": Decimal(str(market.get("stepSize", lot.get("stepSize", "0.000001")))),
            "min": Decimal(str(market.get("minQty", lot.get("minQty", "0")))),
        }
    return out


def _futures_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if (
            s.get("status") != "TRADING"
            or s.get("contractType") != "PERPETUAL"
            or s.get("quoteAsset") != "USDT"
            or s.get("marginAsset") != "USDT"
        ):
            continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {})
        out[s["symbol"]] = {
            "step": Decimal(str(lot.get("stepSize", "0.001"))),
            "min": Decimal(str(lot.get("minQty", "0"))),
        }
    return out


def _requested_universe(sf, ff):
    common = sorted(set(sf) & set(ff))
    configured = os.getenv("FUTURES_SYMBOLS", "").strip()
    if configured:
        requested = {s.strip().upper() for s in configured.split(",") if s.strip()}
        return [s for s in common if s in requested]
    return common


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
        symbols = _requested_universe(sf, ff)
        if not symbols:
            raise FuturesError("no common Spot/USD-M USDT perpetual symbols configured")

        max_symbols = int(os.getenv("FUTURES_MAX_SYMBOLS", "0"))
        if max_symbols > 0:
            symbols = symbols[:max_symbols]

        max_notional = Decimal(os.getenv("FUTURES_MAX_NOTIONAL_USDT", "25"))
        min_edge = Decimal(os.getenv("FUTURES_MIN_NET_EDGE_BPS", "8"))
        fee_bps = Decimal(os.getenv("FUTURES_FEE_BPS", "5"))
        funding_buffer = Decimal(os.getenv("FUTURES_FUNDING_BUFFER_BPS", "2"))
        engine = BasisHedgeEngine(futures, spot, max_notional, leverage=1, live=live)
        print(
            f"DRAGON FUTURES | enabled=True live={live} universe={len(symbols)} "
            f"max_notional={max_notional} leverage=1",
            flush=True,
        )

        while True:
            try:
                # Bulk endpoints keep request volume nearly constant even when the
                # universe grows to hundreds of perpetuals.
                spot_rows = await asyncio.to_thread(spot.public, "/api/v3/ticker/bookTicker")
                fut_rows = await asyncio.to_thread(futures.all_book_tickers)
                marks = await asyncio.to_thread(futures.all_mark_prices)
                spot_by = {row.get("symbol"): row for row in spot_rows if row.get("symbol") in sf}
                fut_by = {row.get("symbol"): row for row in fut_rows if row.get("symbol") in ff}
                mark_by = {row.get("symbol"): row for row in marks if row.get("symbol") in ff}

                for symbol in symbols:
                    try:
                        st = spot_by.get(symbol)
                        ft = fut_by.get(symbol)
                        mark = mark_by.get(symbol)
                        if not st or not ft or not mark:
                            continue
                        sa = Decimal(str(st["askPrice"]))
                        fb = Decimal(str(ft["bidPrice"]))
                        if sa <= 0 or fb <= 0:
                            continue
                        funding = Decimal(str(mark.get("lastFundingRate", "0"))) * Decimal("10000")
                        net = (
                            (fb / sa - Decimal("1")) * Decimal("10000")
                            - fee_bps * Decimal("2")
                            - max(funding, Decimal("0"))
                            - funding_buffer
                        )
                        if net < min_edge:
                            continue

                        positions = await asyncio.to_thread(futures.position_risk, symbol)
                        if any(abs(Decimal(str(p.get("positionAmt", "0")))) > 0 for p in positions):
                            print(f"DRAGON FUTURES | EXISTING_POSITION {symbol}; skip new hedge", flush=True)
                            continue

                        notional = min(
                            max_notional,
                            Decimal(os.getenv("FUTURES_ORDER_NOTIONAL_USDT", str(max_notional))),
                        )
                        qty = _floor(notional / sa, max(sf[symbol]["step"], ff[symbol]["step"]))
                        if qty < sf[symbol]["min"] or qty < ff[symbol]["min"]:
                            print(f"DRAGON FUTURES | FILTER_REJECTED {symbol} qty={qty}", flush=True)
                            continue

                        print(
                            f"DRAGON FUTURES | OPPORTUNITY {symbol} net={net:.3f}bps "
                            f"qty={qty} live={live}",
                            flush=True,
                        )
                        if live:
                            result = await asyncio.to_thread(engine.open_hedge, symbol, qty, sa, net)
                            print(
                                f"DRAGON FUTURES | HEDGE {symbol} status={result.get('futures_order', {}).get('status', 'UNKNOWN')}",
                                flush=True,
                            )
                    except FuturesError as exc:
                        print(f"DRAGON FUTURES | SYMBOL_ERROR {symbol} {exc}", flush=True)
                    except Exception as exc:
                        print(f"DRAGON FUTURES | ERROR {symbol} {exc}", flush=True)
            except FuturesError as exc:
                # In particular, 418/429 responses are converted into a client-side
                # backoff. Do not hammer Binance while an IP ban is active.
                print(f"DRAGON FUTURES | MARKET_DATA_BACKOFF {exc}", flush=True)
            except Exception as exc:
                print(f"DRAGON FUTURES | MARKET_DATA_ERROR {exc}", flush=True)

            await asyncio.sleep(float(os.getenv("FUTURES_POLL_SECONDS", "5")))
    finally:
        spot.close()
        futures.close()
