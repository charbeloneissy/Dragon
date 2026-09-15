from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from typing import Callable

import websockets

VENUES = ("BYBIT", "OKX", "COINBASE")
DEFAULT_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "TRXUSDT","LTCUSDT","BCHUSDT","UNIUSDT","NEARUSDT","ATOMUSDT","APTUSDT","ARBUSDT","OPUSDT","FILUSDT",
    "ETCUSDT","ICPUSDT","INJUSDT","SUIUSDT","SEIUSDT","TONUSDT","HBARUSDT","AAVEUSDT","PEPEUSDT","SHIBUSDT",
    "RENDERUSDT","FETUSDT","TAOUSDT","ENAUSDT","WIFUSDT","JUPUSDT","STXUSDT","IMXUSDT","MKRUSDT","RUNEUSDT",
    "GRTUSDT","ALGOUSDT","XLMUSDT","VETUSDT","EOSUSDT","XTZUSDT","THETAUSDT","CRVUSDT","LDOUSDT",
]


def _okx_id(symbol: str) -> str:
    return symbol[:-4] + "-USDT" if symbol.endswith("USDT") else symbol


def _coinbase_id(symbol: str) -> str:
    return symbol[:-4] + "-USD" if symbol.endswith("USDT") else symbol


def _normalize_usd_symbol(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("-USD"):
        return s[:-4] + "USDT"
    if s.endswith("USD"):
        return s[:-3] + "USDT"
    return s.replace("-", "")


def _fee_bps(venue: str) -> float:
    return float(os.getenv(f"CROSS_{venue}_FEE_BPS", os.getenv("CROSS_FEE_BPS", "10")))


class MultiExchangeFeeds:
    def __init__(self, state: dict, lock, event: Callable[[str, str], None], symbols=None):
        self.state = state
        self.lock = lock
        self.event = event
        env_symbols = [s.strip().upper() for s in os.getenv("CROSS_SYMBOLS", "").split(",") if s.strip()]
        self.symbols = env_symbols or symbols or DEFAULT_SYMBOLS
        self.external_feeds = state.setdefault("external_feeds", {})
        self.cross_exchange_opportunities = state.setdefault("cross_exchange_opportunities", [])
        self._books = {v: {} for v in VENUES}
        self._coinbase_books = {s: {"bid": {}, "offer": {}} for s in self.symbols}
        self._update_times = {v: deque(maxlen=300) for v in VENUES}
        self._calc_times = deque(maxlen=300)
        self._last_calc_mono = 0.0
        self._tasks = []

    def _record_feed(self, venue: str, symbol: str, bid: float, ask: float, bid_qty: float, ask_qty: float):
        now = time.time()
        mono = time.monotonic()
        try:
            bid, ask, bid_qty, ask_qty = map(float, (bid, ask, bid_qty, ask_qty))
        except (TypeError, ValueError):
            return
        if min(bid, ask, bid_qty, ask_qty) <= 0 or bid >= ask:
            return
        self._books[venue][symbol] = {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "ts": now, "mono": mono}
        times = self._update_times[venue]
        times.append(mono)
        window_start = mono - 10.0
        recent = [t for t in times if t >= window_start]
        rate = len(recent) / 10.0
        feed = self.external_feeds.setdefault(venue, {})
        feed.update({"status": "connected", "last_update": now, "quote_age_ms": 0, "updates_per_sec": round(rate, 3)})
        feed["updates"] = int(feed.get("updates", 0)) + 1
        with self.lock:
            self.state["external_feed_updates"] = int(self.state.get("external_feed_updates", 0)) + 1
        self._update_opportunities()

    def _update_opportunities(self):
        stale_ms = float(os.getenv("CROSS_STALE_MS", "1500"))
        min_edge_bps = float(os.getenv("CROSS_MIN_NET_EDGE_BPS", "0.10"))
        slippage_bps = float(os.getenv("CROSS_SLIPPAGE_BPS", "0.50"))
        latency_bps_per_ms = float(os.getenv("CROSS_LATENCY_BPS_PER_MS", "0.005"))
        min_notional = float(os.getenv("CROSS_MIN_DISPLAY_NOTIONAL_USDT", "0.01"))
        recalc_min_ms = float(os.getenv("CROSS_RECALC_MIN_MS", "100"))
        mono = time.monotonic()
        if self._last_calc_mono and (mono - self._last_calc_mono) * 1000.0 < recalc_min_ms:
            return
        self._last_calc_mono = mono
        calc_start = mono
        now = time.time()
        rows = []
        max_age_ms = 0.0
        for symbol in self.symbols:
            quotes = []
            for venue in VENUES:
                q = self._books[venue].get(symbol)
                if not q:
                    continue
                age = (now - q["ts"]) * 1000
                if age <= stale_ms:
                    quotes.append((venue, q, age))
                    max_age_ms = max(max_age_ms, age)
            for buy_venue, buy, buy_age in quotes:
                for sell_venue, sell, sell_age in quotes:
                    if buy_venue == sell_venue:
                        continue
                    gross_bps = (sell["bid"] / buy["ask"] - 1.0) * 10000.0
                    fees_bps = _fee_bps(buy_venue) + _fee_bps(sell_venue)
                    latency_bps = max(buy_age, sell_age) * latency_bps_per_ms
                    net_bps = gross_bps - fees_bps - slippage_bps - latency_bps
                    executable_notional = min(buy["ask"] * buy["ask_qty"], sell["bid"] * sell["bid_qty"])
                    passed = net_bps >= min_edge_bps and executable_notional >= min_notional
                    rows.append({
                        "symbol": symbol, "buy_venue": buy_venue, "sell_venue": sell_venue,
                        "gross_bps": round(gross_bps, 4), "fees_bps": round(fees_bps, 4),
                        "slippage_bps": round(slippage_bps, 4), "latency_bps": round(latency_bps, 4), "net_bps": round(net_bps, 4),
                        "buy_ask": buy["ask"], "sell_bid": sell["bid"],
                        "buy_qty": buy["ask_qty"], "sell_qty": sell["bid_qty"],
                        "executable_notional_usdt": round(executable_notional, 8),
                        "buy_age_ms": round(buy_age, 1), "sell_age_ms": round(sell_age, 1),
                        "gate": "PASS" if passed else "EDGE_OR_LIQUIDITY",
                        "execution_ready": False,
                        "execution_reason": "scanner-only: external two-leg execution disabled",
                    })
        rows.sort(key=lambda r: (r["net_bps"], r["executable_notional_usdt"]), reverse=True)
        calc_ms = (time.monotonic() - calc_start) * 1000.0
        self._calc_times.append(calc_ms)
        calc_rate = len(self._calc_times) / max(1.0, min(300.0, (self._calc_times[-1] - self._calc_times[0]) if len(self._calc_times) > 1 else 1.0))
        with self.lock:
            self.state["cross_exchange_opportunities"] = rows[:50]
            self.state["cross_exchange_last_update"] = now
            self.state["cross_exchange_calc_ms"] = round(calc_ms, 3)
            self.state["cross_exchange_max_quote_age_ms"] = round(max_age_ms, 1)
            self.state["cross_exchange_symbols"] = len(self.symbols)
            self.state["cross_exchange_calc_samples"] = len(self._calc_times)
            self.state["cross_exchange_last_net_bps"] = rows[0]["net_bps"] if rows else None
            self.state["cross_exchange_calc_rate_per_sec"] = round(calc_rate, 3)
        if rows and rows[0]["gate"] == "PASS":
            top = rows[0]
            self.event("CROSS_OPPORTUNITY", f"{top['symbol']} {top['buy_venue']}->{top['sell_venue']} net_bps={top['net_bps']}")

    async def _run_venue(self, venue, url, messages, parser):
        delay = 1.0
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, open_timeout=15, max_size=2**24) as ws:
                    self.external_feeds.setdefault(venue, {})["status"] = "connected"
                    self.event("EXT_WS", f"{venue} connected")
                    delay = 1.0
                    for msg in messages:
                        await ws.send(json.dumps(msg))
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            for symbol, bid, ask, bid_qty, ask_qty in parser(data) or ():
                                self._record_feed(venue, symbol, bid, ask, bid_qty, ask_qty)
                        except Exception as exc:
                            feed = self.external_feeds.setdefault(venue, {})
                            feed["parse_errors"] = int(feed.get("parse_errors", 0)) + 1
                            self.event("EXT_PARSE_ERROR", f"{venue}: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                feed = self.external_feeds.setdefault(venue, {})
                feed["status"] = "reconnecting"
                feed["last_error"] = str(exc)
                feed["reconnects"] = int(feed.get("reconnects", 0)) + 1
                self.event("EXT_WS_ERROR", f"{venue}: {exc}")
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    def _bybit_messages(self):
        return [{"op": "subscribe", "args": [f"orderbook.1.{s}" for s in self.symbols[i:i + 10]]} for i in range(0, len(self.symbols), 10)]

    def _bybit_parser(self, msg):
        if msg.get("topic", "").startswith("orderbook."):
            d = msg.get("data", {})
            s = d.get("s")
            b, a = d.get("b", []), d.get("a", [])
            if s and b and a:
                yield s.upper(), float(b[0][0]), float(a[0][0]), float(b[0][1]), float(a[0][1])

    def _okx_messages(self):
        return [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": _okx_id(s)} for s in self.symbols]}]

    def _okx_parser(self, msg):
        if msg.get("arg", {}).get("channel") == "bbo-tbt":
            for d in msg.get("data", []):
                inst = d.get("instId", "")
                bids, asks = d.get("bids"), d.get("asks")
                if bids and asks:
                    yield _normalize_usd_symbol(inst), float(bids[0][0]), float(bids[0][1]), float(bids[0][1]), float(asks[0][1])

    def _coinbase_messages(self):
        return [
            {"type": "subscribe", "channel": "level2", "product_ids": [_coinbase_id(s) for s in self.symbols]},
            {"type": "subscribe", "channel": "heartbeats"},
        ]

    def _coinbase_parser(self, msg):
        if msg.get("channel") != "l2_data":
            return
        for event in msg.get("events", []):
            product_id = event.get("product_id", "")
            symbol = _normalize_usd_symbol(product_id)
            book = self._coinbase_books.setdefault(symbol, {"bid": {}, "offer": {}})
            for update in event.get("updates", []):
                side = "bid" if update.get("side") == "bid" else "offer"
                price = float(update.get("price_level", 0))
                qty = float(update.get("new_quantity", 0))
                if price <= 0:
                    continue
                if qty <= 0:
                    book[side].pop(price, None)
                else:
                    book[side][price] = qty
            if book["bid"] and book["offer"]:
                bid = max(book["bid"])
                ask = min(book["offer"])
                if bid < ask:
                    yield symbol, bid, ask, book["bid"][bid], book["offer"][ask]

    async def run(self):
        configs = [
            ("BYBIT", "wss://stream.bybit.com/v5/public/spot", self._bybit_messages(), self._bybit_parser),
            ("OKX", "wss://ws.okx.com:8443/ws/v5/public", self._okx_messages(), self._okx_parser),
            ("COINBASE", "wss://advanced-trade-ws.coinbase.com", self._coinbase_messages(), self._coinbase_parser),
        ]
        self._tasks = [asyncio.create_task(self._run_venue(*cfg)) for cfg in configs]
        try:
            await asyncio.gather(*self._tasks)
        finally:
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
