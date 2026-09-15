from __future__

import asyncio
import json
import os
import random
import time
from collections import deque
from typing import Callable
from urllib.request import Request, urlopen

import websockets

VENUES = ("BYBIT", "OKX", "COINBASE")
DEFAULT_SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT"]

WS_BACKOFF_MIN = 1.0
WS_BACKOFF_MAX = 60.0
WS_STALE_SECONDS = float(os.getenv("CROSS_WS_STALE_SECONDS", "45"))
WS_PING_INTERVAL = float(os.getenv("CROSS_WS_PING_INTERVAL", "15"))
WS_PING_TIMEOUT = float(os.getenv("CROSS_WS_PING_TIMEOUT", "10"))
WS_OPEN_TIMEOUT = float(os.getenv("CROSS_WS_OPEN_TIMEOUT", "15"))
WS_CLOSE_TIMEOUT = float(os.getenv("CROSS_WS_CLOSE_TIMEOUT", "5"))
WS_MAX_QUEUE = int(os.getenv("CROSS_WS_MAX_QUEUE", "4096"))
WS_MAX_SIZE = int(os.getenv("CROSS_WS_MAX_SIZE", str(2**24)))
WS_SYMBOLS_PER_CONNECTION = max(50, int(os.getenv("CROSS_WS_SYMBOLS_PER_CONNECTION", "250")))
WS_SUBSCRIBE_DELAY = max(0.0, float(os.getenv("CROSS_WS_SUBSCRIBE_DELAY", "0.10")))
WS_APP_HEARTBEAT_SECONDS = max(5.0, float(os.getenv("CROSS_WS_APP_HEARTBEAT_SECONDS", "20")))


def _http_json(url: str, timeout: float = 10.0):
    req = Request(url, headers={"User-Agent": "Dragon/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class MultiExchangeFeeds:
    """Observation-only cross-venue scanner with bounded, perpetual reconnects."""

    def __init__(self, state, lock, event: Callable | None = None, symbols=None):
        self.state = state
        self.lock = lock
        self.event = event
        requested = symbols or DEFAULT_SYMBOLS
        override = os.getenv("CROSS_SYMBOLS", "").strip()
        if override:
            requested = [x.strip().upper() for x in override.split(",") if x.strip()]
        self.symbols = list(dict.fromkeys(requested))[:1000]
        self.venue_symbols = {v: set(self.symbols) for v in VENUES}
        self._books = {v: {} for v in VENUES}
        self._update_times = {v: deque(maxlen=100) for v in VENUES}
        self._last_update = {v: 0.0 for v in VENUES}
        self._reconnects = {v: 0 for v in VENUES}
        self._tasks = []
        self._calc_count = 0
        self._calc_started_mono = time.monotonic()
        self._last_calc_mono = 0.0

    def _emit(self, message: str):
        if self.event:
            try:
                self.event(message)
            except Exception:
                pass

    def _record_feed(self, venue, symbol, bid, ask, bid_qty, ask_qty):
        try:
            bid, ask, bid_qty, ask_qty = map(float, (bid, ask, bid_qty, ask_qty))
        except (TypeError, ValueError):
            return
        if min(bid, ask, bid_qty, ask_qty) <= 0 or ask < bid:
            return
        now = time.monotonic()
        self._books[venue][symbol] = {
            "bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty,
            "ts": now,
        }
        self._update_times[venue].append(now)
        self._last_update[venue] = now
        ups = 0.0
        times = self._update_times[venue]
        if len(times) >= 2:
            span = max(times[-1] - times[0], 1e-6)
            ups = (len(times) - 1) / span
        with self.lock:
            ext = self.state.setdefault("external", {})
            feeds = ext.setdefault("feeds", {})
            feeds.setdefault(venue, {})[symbol] = {
                "bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty,
                "quote_age_ms": 0.0, "updates_per_sec": ups,
                "last_update": time.time(),
            }

    def _record_control(self, venue, msg):
        if venue == "BYBIT" and msg.get("op") == "subscribe":
            ok = bool(msg.get("success"))
            self._emit(f"EXT_SUB_{'OK' if ok else 'ERROR'} | BYBIT | {msg.get('ret_msg', '')}")

    def _bybit_messages(self, symbols):
        ordered = list(dict.fromkeys(["BTCUSDT"] + list(symbols)))
        return [{"op": "subscribe", "args": [f"orderbook.1.{s}" for s in ordered[i:i + 10]]}
                for i in range(0, len(ordered), 10)]

    def _okx_messages(self, symbols):
        return [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": f"{s[:-4]}-USDT"} for s in symbols[i:i + 100]]}
                for i in range(0, len(symbols), 100)]

    def _coinbase_messages(self, symbols):
        products = [f"{s[:-4]}-USD" for s in symbols]
        return [{"type": "subscribe", "product_ids": products[i:i + 100], "channel": "level2"}
                for i in range(0, len(products), 100)]

    def _bybit_parser(self, msg):
        topic = msg.get("topic", "")
        if not topic.startswith("orderbook.1."):
            return
        data = msg.get("data") or {}
        symbol = data.get("s")
        bids, asks = data.get("b") or [], data.get("a") or []
        if symbol and bids and asks:
            yield symbol.upper(), float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1])

    def _okx_parser(self, msg):
        if msg.get("arg", {}).get("channel") != "bbo-tbt":
            return
        for d in msg.get("data") or []:
            inst = d.get("instId", "")
            if not inst.endswith("-USDT"):
                continue
            bids, asks = d.get("bids") or [], d.get("asks") or []
            if bids and asks:
                yield inst.replace("-USDT", "") + "USDT", float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1])

    def _coinbase_parser(self, msg):
        if msg.get("channel") != "l2_data":
            return
        for ev in msg.get("events") or []:
            for book in ev.get("updates") or []:
                product = book.get("product_id", "")
                if not product.endswith("-USD"):
                    continue
                price = float(book.get("price", 0) or 0)
                qty = float(book.get("new_quantity", 0) or 0)
                if price <= 0:
                    continue
                symbol = product.replace("-USD", "") + "USDT"
                side = book.get("side")
                cb = getattr(self, "_coinbase_books", {})
                self._coinbase_books = cb
                levels = cb.setdefault(symbol, {"bid": {}, "ask": {}})
                levels["bid" if side == "bid" else "ask"][price] = qty
                if qty <= 0:
                    levels["bid" if side == "bid" else "ask"].pop(price, None)
                bids, asks = levels["bid"], levels["ask"]
                if bids and asks:
                    bp = max(bids); ap = min(asks)
                    yield symbol, bp, ap, bids[bp], asks[ap]

    async def _run_venue(self, venue, symbols, connection_no):
        urls = {"BYBIT": "wss://stream.bybit.com/v5/public/spot", "OKX": "wss://ws.okx.com:8443/ws/v5/public", "COINBASE": "wss://advanced-trade-ws.coinbase.com"}
        builders = {"BYBIT": self._bybit_messages, "OKX": self._okx_messages, "COINBASE": self._coinbase_messages}
        parsers = {"BYBIT": self._bybit_parser, "OKX": self._okx_parser, "COINBASE": self._coinbase_parser}
        delay = WS_BACKOFF_MIN
        while True:
            try:
                self._emit(f"EXT_WS | {venue} connection-{connection_no} connecting")
                async with websockets.connect(urls[venue], ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT,
                                               close_timeout=WS_CLOSE_TIMEOUT, open_timeout=WS_OPEN_TIMEOUT,
                                               max_size=WS_MAX_SIZE, max_queue=WS_MAX_QUEUE, compression=None) as ws:
                    for msg in builders[venue](symbols):
                        await ws.send(json.dumps(msg))
                        await asyncio.sleep(WS_SUBSCRIBE_DELAY)
                    self._emit(f"EXT_WS | {venue} connection-{connection_no} connected")
                    delay = WS_BACKOFF_MIN
                    last_data = time.monotonic()
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=WS_STALE_SECONDS)
                            if raw is None:
                                raise ConnectionError("websocket closed")
                            msg = json.loads(raw)
                            self._record_control(venue, msg)
                            got = False
                            for item in parsers[venue](msg) or ():
                                self._record_feed(venue, *item)
                                got = True
                            if got:
                                last_data = time.monotonic()
                        except asyncio.TimeoutError:
                            raise ConnectionError(f"stale feed > {WS_STALE_SECONDS:.0f}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._reconnects[venue] += 1
                self._emit(f"EXT_WS_ERROR | {venue} connection-{connection_no} | {type(exc).__name__}: {exc} | reconnect #{self._reconnects[venue]}")
                await asyncio.sleep(min(delay, WS_BACKOFF_MAX) * (0.8 + random.random() * 0.4))
                delay = min(delay * 2.0, WS_BACKOFF_MAX)

    def _update_opportunities(self):
        stale_ms = float(os.getenv("CROSS_STALE_MS", "1500"))
        max_sync_ms = float(os.getenv("CROSS_MAX_SYNC_SKEW_MS", "500"))
        min_net = float(os.getenv("CROSS_MIN_NET_EDGE_BPS", "0.10"))
        slippage = float(os.getenv("CROSS_SLIPPAGE_BPS", "0.50"))
        latency_bps_ms = float(os.getenv("CROSS_LATENCY_BPS_PER_MS", "0.005"))
        evaluation = float(os.getenv("CROSS_EVALUATION_NOTIONAL_USDT", "5"))
        start = time.monotonic()
        rows = []
        now = time.monotonic()
        for symbol in self.symbols:
            for buy_venue in VENUES:
                b = self._books[buy_venue].get(symbol)
                if not b:
                    continue
                for sell_venue in VENUES:
                    if sell_venue == buy_venue:
                        continue
                    s = self._books[sell_venue].get(symbol)
                    if not s:
                        continue
                    buy_age = (now - b["ts"]) * 1000
                    sell_age = (now - s["ts"]) * 1000
                    skew = abs(b["ts"] - s["ts"]) * 1000
                    if max(buy_age, sell_age) > stale_ms or skew > max_sync_ms:
                        continue
                    gross = (s["bid"] / b["ask"] - 1.0) * 10000
                    fees = 20.0
                    latency = max(buy_age, sell_age) * latency_bps_ms
                    net = gross - fees - slippage - latency
                    notional = min(evaluation, b["ask"] * b["ask_qty"], s["bid"] * s["bid_qty"])
                    rows.append({"symbol": symbol, "buy_venue": buy_venue, "sell_venue": sell_venue,
                                  "gross_edge_bps": gross, "fee_bps": fees, "slippage_bps": slippage,
                                  "latency_penalty_bps": latency, "net_edge_bps": net,
                                  "executable_notional_usdt": max(0.0, notional),
                                  "expected_pnl_usdt": max(0.0, notional) * net / 10000.0,
                                  "gate": "PASS" if net >= min_net and notional > 0 else "EDGE_OR_LIQUIDITY"})
        rows.sort(key=lambda x: x["net_edge_bps"], reverse=True)
        elapsed = (time.monotonic() - start) * 1000
        self._calc_count += 1
        self._last_calc_mono = time.monotonic()
        with self.lock:
            ext = self.state.setdefault("external", {})
            ext["opportunities"] = rows[:50]
            ext["calculation_ms"] = elapsed
            ext["calculations_per_sec"] = self._calc_count / max(time.monotonic() - self._calc_started_mono, 1e-6)
            ext["reconnects"] = dict(self._reconnects)
            ext["supported_symbols"] = {v: len(self.venue_symbols[v]) for v in VENUES}

    async def _calculator(self):
        interval = max(0.05, float(os.getenv("CROSS_RECALC_MIN_MS", "100")) / 1000.0)
        while True:
            self._update_opportunities()
            await asyncio.sleep(interval)

    async def run(self):
        self._coinbase_books = {}
        tasks = [asyncio.create_task(self._calculator())]
        for venue in VENUES:
            for i in range(0, len(self.symbols), WS_SYMBOLS_PER_CONNECTION):
                shard = self.symbols[i:i + WS_SYMBOLS_PER_CONNECTION]
                tasks.append(asyncio.create_task(self._run_venue(venue, shard, i // WS_SYMBOLS_PER_CONNECTION + 1)))
        self._tasks = tasks
        await asyncio.gather(*tasks)
