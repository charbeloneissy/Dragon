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
DEFAULT_SYMBOLS = [
    "BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT",
    "TRXUSDT","LTCUSDT","BCHUSDT","UNIUSDT","NEARUSDT","ATOMUSDT","APTUSDT","ARBUSDT","OPUSDT","FILUSDT",
    "ETCUSDT","ICPUSDT","INJUSDT","SUIUSDT","SEIUSDT","TONUSDT","HBARUSDT","AAVEUSDT","PEPEUSDT","SHIBUSDT",
    "RENDERUSDT","FETUSDT","TAOUSDT","ENAUSDT","WIFUSDT","JUPUSDT","STXUSDT","IMXUSDT","MKRUSDT","RUNEUSDT",
    "GRTUSDT","ALGOUSDT","XLMUSDT","VETUSDT","EOSUSDT","XTZUSDT","THETAUSDT","CRVUSDT","LDOUSDT",
]

WS_BACKOFF_MIN = 1.0
WS_BACKOFF_MAX = 60.0
WS_STALE_SECONDS = float(os.getenv("CROSS_WS_STALE_SECONDS", "45"))
WS_PING_INTERVAL = float(os.getenv("CROSS_WS_PING_INTERVAL", "15"))
WS_PING_TIMEOUT = float(os.getenv("CROSS_WS_PING_TIMEOUT", "10"))
WS_OPEN_TIMEOUT = float(os.getenv("CROSS_WS_OPEN_TIMEOUT", "15"))
WS_CLOSE_TIMEOUT = float(os.getenv("CROSS_WS_CLOSE_TIMEOUT", "5"))
WS_MAX_QUEUE = int(os.getenv("CROSS_WS_MAX_QUEUE", str(4096)))
WS_MAX_SIZE = int(os.getenv("CROSS_WS_MAX_SIZE", str(2**24)))
WS_SYMBOLS_PER_CONNECTION = max(50, int(os.getenv("CROSS_WS_SYMBOLS_PER_CONNECTION", "250")))
WS_SUBSCRIBE_DELAY = max(0.0, float(os.getenv("CROSS_WS_SUBSCRIBE_DELAY", "0.10")))
WS_APP_HEARTBEAT_SECONDS = max(5.0, float(os.getenv("CROSS_WS_APP_HEARTBEAT_SECONDS", "20")))


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
    if s.endswith("-USDT"):
        return s[:-5] + "USDT"
    return s.replace("-", "")


def _fee_bps(venue: str) -> float:
    return float(os.getenv(f"CROSS_{venue}_FEE_BPS", os.getenv("CROSS_FEE_BPS", "10")))


def _http_json(url: str) -> dict | list:
    req = Request(url, headers={"User-Agent": "Dragon/1.0"})
    with urlopen(req, timeout=12) as r:
        return json.loads(r.read().decode("utf-8"))


class MultiExchangeFeeds:
    """Observation-only cross-exchange scanner with venue-specific symbols."""

    def __init__(self, state: dict, lock, event: Callable[[str, str], None], symbols=None):
        self.state = state
        self.lock = lock
        self.event = event
        env_symbols = [s.strip().upper() for s in os.getenv("CROSS_SYMBOLS", "").split(",") if s.strip()]
        source_symbols = env_symbols or symbols or DEFAULT_SYMBOLS
        self.symbols = list(dict.fromkeys(source_symbols))[:1000]
        self.external_feeds = state.setdefault("external_feeds", {})
        self.cross_exchange_opportunities = state.setdefault("cross_exchange_opportunities", [])
        self._books = {v: {} for v in VENUES}
        self._coinbase_books = {}
        self._update_times = {v: deque(maxlen=300) for v in VENUES}
        self._calc_count = 0
        self._calc_started_mono = time.monotonic()
        self._last_calc_mono = 0.0
        self._tasks = []
        self.venue_symbols = {v: [] for v in VENUES}

    def _feed(self, venue):
        return self.external_feeds.setdefault(venue, {})

    async def _discover_symbols(self):
        requested = set(s for s in self.symbols if s.endswith("USDT"))

        def bybit():
            out = set(); cursor = ""
            for _ in range(5):
                url = "https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000"
                if cursor:
                    url += "&cursor=" + cursor
                data = _http_json(url)
                for x in (data.get("result", {}).get("list") or []):
                    if x.get("status") == "Trading" and x.get("quoteCoin") == "USDT":
                        out.add(str(x.get("symbol", "")).upper())
                cursor = str(data.get("result", {}).get("nextPageCursor") or "")
                if not cursor:
                    break
            return out

        def okx():
            data = _http_json("https://www.okx.com/api/v5/public/instruments?instType=SPOT")
            return {str(x.get("instId", "")).replace("-", "").upper() for x in (data.get("data") or []) if x.get("state") in (None, "live") and x.get("quoteCcy") == "USDT"}

        def coinbase():
            data = _http_json("https://api.exchange.coinbase.com/products")
            return {str(x.get("id", "")).replace("-", "").upper().replace("USD", "USDT") for x in (data or []) if str(x.get("quote_currency", "")).upper() == "USD" and not bool(x.get("trading_disabled")) and str(x.get("status", "")).lower() in ("online", "active", "")}

        jobs = {"BYBIT": bybit, "OKX": okx, "COINBASE": coinbase}
        for venue, fn in jobs.items():
            try:
                supported = await asyncio.to_thread(fn)
                self.venue_symbols[venue] = sorted(requested & supported)
                self._feed(venue).update({"supported_symbols": len(self.venue_symbols[venue]), "requested_symbols": len(requested), "symbol_filter_ready": True, "last_symbol_refresh": time.time()})
            except Exception as exc:
                fallback = sorted(requested & set(DEFAULT_SYMBOLS))
                self.venue_symbols[venue] = fallback
                self._feed(venue).update({"supported_symbols": len(fallback), "requested_symbols": len(requested), "symbol_filter_ready": False, "last_symbol_refresh_error": str(exc)})
                self.event("EXT_SYMBOL_DISCOVERY_ERROR", f"{venue}: {exc}")

        for symbol in set(sum(self.venue_symbols.values(), [])):
            self._coinbase_books.setdefault(symbol, {"bid": {}, "offer": {}})
        with self.lock:
            self.state["external_supported_symbols"] = {v: len(self.venue_symbols[v]) for v in VENUES}
            self.state["external_requested_symbols"] = len(requested)

    def _record_feed(self, venue, symbol, bid, ask, bid_qty, ask_qty):
        now = time.time(); mono = time.monotonic()
        try:
            bid, ask, bid_qty, ask_qty = map(float, (bid, ask, bid_qty, ask_qty))
        except (TypeError, ValueError):
            return
        if min(bid, ask, bid_qty, ask_qty) <= 0 or bid >= ask:
            return
        self._books[venue][symbol] = {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "ts": now, "mono": mono}
        times = self._update_times[venue]; times.append(mono)
        recent = [t for t in times if t >= mono - 10.0]
        feed = self._feed(venue)
        feed.update({"status": "connected", "last_update": now, "quote_age_ms": 0, "updates_per_sec": round(len(recent) / 10.0, 3)})
        feed["updates"] = int(feed.get("updates", 0)) + 1
        with self.lock:
            self.state["external_feed_updates"] = int(self.state.get("external_feed_updates", 0)) + 1
        self._update_opportunities()

    def _update_opportunities(self):
        stale_ms = float(os.getenv("CROSS_STALE_MS", "1500"))
        sync_ms = float(os.getenv("CROSS_MAX_SYNC_SKEW_MS", "500"))
        min_edge_bps = float(os.getenv("CROSS_MIN_NET_EDGE_BPS", "0.10"))
        slippage_bps = max(0.0, float(os.getenv("CROSS_SLIPPAGE_BPS", "0.50")))
        latency_bps_per_ms = max(0.0, float(os.getenv("CROSS_LATENCY_BPS_PER_MS", "0.005")))
        min_notional = max(0.01, float(os.getenv("CROSS_MIN_DISPLAY_NOTIONAL_USDT", "0.01")))
        eval_notional = max(min_notional, float(os.getenv("CROSS_EVALUATION_NOTIONAL_USDT", "5")))
        recalc_min_ms = max(0.0, float(os.getenv("CROSS_RECALC_MIN_MS", "100")))
        mono = time.monotonic()
        if self._last_calc_mono and (mono - self._last_calc_mono) * 1000.0 < recalc_min_ms:
            return
        self._last_calc_mono = mono; calc_start = mono; now = time.time(); rows = []; max_age_ms = 0.0
        for symbol in self.symbols:
            quotes = []
            for venue in VENUES:
                q = self._books[venue].get(symbol)
                if not q: continue
                age = max(0.0, (now - q["ts"]) * 1000.0)
                if age <= stale_ms:
                    quotes.append((venue, q, age)); max_age_ms = max(max_age_ms, age)
            for buy_venue, buy, buy_age in quotes:
                for sell_venue, sell, sell_age in quotes:
                    if buy_venue == sell_venue: continue
                    sync_skew = abs(buy["ts"] - sell["ts"]) * 1000.0
                    if sync_skew > sync_ms or buy["ask"] <= 0 or sell["bid"] <= 0: continue
                    gross_bps = (sell["bid"] / buy["ask"] - 1.0) * 10000.0
                    fees_bps = _fee_bps(buy_venue) + _fee_bps(sell_venue)
                    latency_bps = max(buy_age, sell_age) * latency_bps_per_ms
                    executable_notional = min(buy["ask"] * buy["ask_qty"], sell["bid"] * sell["bid_qty"], eval_notional)
                    net_bps = gross_bps - fees_bps - slippage_bps - latency_bps
                    expected_pnl = executable_notional * net_bps / 10000.0
                    gate = "PASS" if net_bps >= min_edge_bps and executable_notional >= min_notional else "EDGE_OR_LIQUIDITY"
                    rows.append({"symbol": symbol, "buy_venue": buy_venue, "sell_venue": sell_venue, "gross_bps": round(gross_bps, 4), "fees_bps": round(fees_bps, 4), "slippage_bps": round(slippage_bps, 4), "latency_bps": round(latency_bps, 4), "net_bps": round(net_bps, 4), "expected_pnl_usdt": round(expected_pnl, 8), "buy_ask": buy["ask"], "sell_bid": sell["bid"], "buy_qty": buy["ask_qty"], "sell_qty": sell["bid_qty"], "executable_notional_usdt": round(executable_notional, 8), "buy_age_ms": round(buy_age, 1), "sell_age_ms": round(sell_age, 1), "sync_skew_ms": round(sync_skew, 1), "gate": gate, "execution_ready": False, "execution_reason": "scanner-only: external two-leg execution disabled"})
        rows.sort(key=lambda r: (r["net_bps"], r["expected_pnl_usdt"], r["executable_notional_usdt"]), reverse=True)
        calc_ms = (time.monotonic() - calc_start) * 1000.0; self._calc_count += 1; elapsed = max(1.0, time.monotonic() - self._calc_started_mono)
        with self.lock:
            self.state["cross_exchange_opportunities"] = rows[:50]; self.state["cross_exchange_last_update"] = now; self.state["cross_exchange_calc_ms"] = round(calc_ms, 3); self.state["cross_exchange_max_quote_age_ms"] = round(max_age_ms, 1); self.state["cross_exchange_symbols"] = len(self.symbols); self.state["cross_exchange_calc_samples"] = self._calc_count; self.state["cross_exchange_last_net_bps"] = rows[0]["net_bps"] if rows else None; self.state["cross_exchange_calc_rate_per_sec"] = round(self._calc_count / elapsed, 3); self.state["cross_exchange_pass_count"] = sum(1 for r in rows if r["gate"] == "PASS")
        if rows and rows[0]["gate"] == "PASS":
            top = rows[0]; self.event("CROSS_OPPORTUNITY", f"{top['symbol']} {top['buy_venue']}->{top['sell_venue']} net_bps={top['net_bps']} expected=${top['expected_pnl_usdt']}")

    async def _heartbeat(self, ws, venue):
        if venue == "COINBASE": return
        while True:
            await asyncio.sleep(WS_APP_HEARTBEAT_SECONDS)
            try: await ws.send(json.dumps({"op": "ping"}))
            except Exception: return

    def _record_control(self, venue, data, connection_id):
        feed = self._feed(venue); feed["control_messages"] = int(feed.get("control_messages", 0)) + 1; feed["last_control"] = data
        if venue == "BYBIT" and data.get("op") == "subscribe":
            if data.get("success") is True: feed["subscription_acks"] = int(feed.get("subscription_acks", 0)) + 1
            else:
                feed["subscription_errors"] = int(feed.get("subscription_errors", 0)) + 1; feed["last_subscription_error"] = data.get("ret_msg") or data.get("retCode") or data; self.event("EXT_SUB_ERROR", f"BYBIT connection-{connection_id}: {feed['last_subscription_error']}")

    async def _run_venue(self, venue, url, messages, parser, connection_id):
        delay = WS_BACKOFF_MIN; feed = self._feed(venue); feed.setdefault("connections", 0); feed.setdefault("connected_connections", 0)
        while True:
            heartbeat_task = None; connected = False
            try:
                feed["status"] = "connecting"; feed["connection_id"] = connection_id; self.event("EXT_WS", f"{venue} connection-{connection_id} connecting")
                async with websockets.connect(url, ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT, close_timeout=WS_CLOSE_TIMEOUT, open_timeout=WS_OPEN_TIMEOUT, max_size=WS_MAX_SIZE, max_queue=WS_MAX_QUEUE, compression=None) as ws:
                    connected = True; feed["connections"] = max(int(feed.get("connections", 0)), connection_id); feed["connected_connections"] = int(feed.get("connected_connections", 0)) + 1; feed["last_connect"] = time.time(); feed["last_error"] = None; feed["status"] = "connected"; feed["subscriptions_sent"] = 0; self.event("EXT_WS", f"{venue} connection-{connection_id} connected"); delay = WS_BACKOFF_MIN
                    for msg in messages:
                        await ws.send(json.dumps(msg)); feed["subscriptions_sent"] = int(feed.get("subscriptions_sent", 0)) + len(msg.get("args", msg.get("product_ids", [])))
                        if WS_SUBSCRIBE_DELAY: await asyncio.sleep(WS_SUBSCRIBE_DELAY)
                    heartbeat_task = asyncio.create_task(self._heartbeat(ws, venue)); last_message = time.monotonic()
                    while True:
                        try: raw = await asyncio.wait_for(ws.recv(), timeout=WS_STALE_SECONDS)
                        except asyncio.TimeoutError as exc:
                            raise ConnectionError(f"{venue} market data stale for {time.monotonic() - last_message:.1f}s; forcing websocket reconnect") from exc
                        last_message = time.monotonic(); data = json.loads(raw); self._record_control(venue, data, connection_id); parsed = False
                        try:
                            for symbol, bid, ask, bid_qty, ask_qty in parser(data) or ():
                                parsed = True; self._record_feed(venue, symbol, bid, ask, bid_qty, ask_qty)
                        except Exception as exc:
                            feed["parse_errors"] = int(feed.get("parse_errors", 0)) + 1; self.event("EXT_PARSE_ERROR", f"{venue} connection-{connection_id}: {exc}")
                        if parsed: feed["last_data_message"] = time.time()
            except asyncio.CancelledError:
                feed["status"] = "stopped"; raise
            except Exception as exc:
                feed["status"] = "reconnecting"; feed["last_error"] = str(exc); feed["reconnects"] = int(feed.get("reconnects", 0)) + 1; self.event("EXT_WS_ERROR", f"{venue} connection-{connection_id}: {exc}"); wait = min(WS_BACKOFF_MAX, delay + random.uniform(0.0, min(5.0, delay * 0.25))); feed["next_retry_at"] = time.time() + wait; await asyncio.sleep(wait); delay = min(WS_BACKOFF_MAX, delay * 2.0)
            finally:
                if heartbeat_task: heartbeat_task.cancel(); await asyncio.gather(heartbeat_task, return_exceptions=True)
                if connected: feed["connected_connections"] = max(0, int(feed.get("connected_connections", 1)) - 1)

    def _bybit_messages(self, symbols):
        ordered = list(dict.fromkeys(["BTCUSDT"] + list(symbols)))
        return [{"op": "subscribe", "args": [f"orderbook.1.{s}" for s in ordered[i:i + 10]]} for i in range(0, len(ordered), 10)]

    def _bybit_parser(self, msg):
        topic = msg.get("topic", "")
        if not topic.startswith("orderbook.1."): return
        d = msg.get("data") or {}; s = d.get("s"); bids, asks = d.get("b") or [], d.get("a") or []
        if s and bids and asks: yield s.upper(), float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1])

    def _okx_messages(self, symbols):
        return [{"op": "subscribe", "args": [{"channel": "bbo-tbt", "instId": _okx_id(s)} for s in symbols[i:i + 100]]} for i in range(0, len(symbols), 100)]

    def _okx_parser(self, msg):
        if msg.get("arg", {}).get("channel") == "bbo-tbt":
            for d in msg.get("data", []):
                bids, asks = d.get("bids"), d.get("asks")
                if bids and asks: yield _normalize_usd_symbol(d.get("instId", "")), float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1])

    def _coinbase_messages(self, symbols):
        return [{"type": "subscribe", "channel": "level2", "product_ids": [_coinbase_id(s) for s in symbols[i:i + 100]]} for i in range(0, len(symbols), 100)] + [{"type": "subscribe", "channel": "heartbeats"}]

    def _coinbase_parser(self, msg):
        if msg.get("channel") != "l2_data": return
        for event in msg.get("events", []):
            symbol = _normalize_usd_symbol(event.get("product_id", "")); book = self._coinbase_books.setdefault(symbol, {"bid": {}, "offer": {}})
            for update in event.get("updates", []):
                side = "bid" if update.get("side") == "bid" else "offer"
                try: price = float(update.get("price_level", 0)); qty = float(update.get("new_quantity", 0))
                except (TypeError, ValueError): continue
                if price <= 0: continue
                if qty <= 0: book[side].pop(price, None)
                else: book[side][price] = qty
            if book["bid"] and book["offer"]:
                bid, ask = max(book["bid"]), min(book["offer"])
                if bid < ask: yield symbol, bid, ask, book["bid"][bid], book["offer"][ask]

    async def run(self):
        await self._discover_symbols(); configs = []
        for venue in VENUES:
            symbols = self.venue_symbols.get(venue, []); shards = [symbols[i:i + WS_SYMBOLS_PER_CONNECTION] for i in range(0, len(symbols), WS_SYMBOLS_PER_CONNECTION)] or [[]]
            for idx, shard in enumerate(shards, 1):
                if venue == "BYBIT": cfg = ("BYBIT", "wss://stream.bybit.com/v5/public/spot", self._bybit_messages(shard), self._bybit_parser, idx)
                elif venue == "OKX": cfg = ("OKX", "wss://ws.okx.com:8443/ws/v5/public", self._okx_messages(shard), self._okx_parser, idx)
                else: cfg = ("COINBASE", "wss://advanced-trade-ws.coinbase.com", self._coinbase_messages(shard), self._coinbase_parser, idx)
                configs.append(cfg)
        self._tasks = [asyncio.create_task(self._run_venue(*cfg)) for cfg in configs]
        with self.lock:
            self.state["external_ws_connections_target"] = len(configs); self.state["external_ws_symbols_per_connection"] = WS_SYMBOLS_PER_CONNECTION; self.state["external_venue_shards"] = {v: (len(self.venue_symbols[v]) + WS_SYMBOLS_PER_CONNECTION - 1) // WS_SYMBOLS_PER_CONNECTION for v in VENUES}
        try: await asyncio.gather(*self._tasks)
        finally:
            for task in self._tasks: task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
