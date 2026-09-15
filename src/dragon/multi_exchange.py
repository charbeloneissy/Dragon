from __future__ import annotations

import asyncio
import json
import time
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


class MultiExchangeFeeds:
    def __init__(self, state: dict, lock, event: Callable[[str, str], None], symbols=None):
        self.state = state
        self.lock = lock
        self.event = event
        self.symbols = symbols or DEFAULT_SYMBOLS
        self.external_feeds = state.setdefault("external_feeds", {})
        self.external_feed_updates = state.setdefault("external_feed_updates", 0)
        self.cross_exchange_opportunities = state.setdefault("cross_exchange_opportunities", [])
        self._books = {v: {} for v in VENUES}
        self._coinbase_books = {s: {"bid": {}, "offer": {}} for s in self.symbols}
        self._tasks = []

    def _record_feed(self, venue: str, symbol: str, bid: float, ask: float, bid_qty: float, ask_qty: float):
        now = time.time()
        if not all(x is not None for x in (bid, ask, bid_qty, ask_qty)):
            return
        if min(bid, ask, bid_qty, ask_qty) <= 0 or bid >= ask:
            return
        self._books[venue][symbol] = {
            "bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "ts": now,
        }
        feed = self.external_feeds.setdefault(venue, {})
        feed["status"] = "connected"
        feed["updates"] = int(feed.get("updates", 0)) + 1
        feed["last_update"] = now
        feed["quote_age_ms"] = 0
        self.external_feed_updates += 1
        self._update_opportunities()

    def _update_opportunities(self):
        stale_ms = float(__import__("os").getenv("CROSS_STALE_MS", "3000"))
        min_edge_bps = float(__import__("os").getenv("CROSS_MIN_NET_EDGE_BPS", "0.10"))
        fee_bps = float(__import__("os").getenv("CROSS_FEE_BPS", "10"))
        min_notional = float(__import__("os").getenv("CROSS_MIN_DISPLAY_NOTIONAL_USDT", "0.01"))
        now = time.time()
        rows = []
        for symbol in self.symbols:
            quotes = []
            for venue in VENUES:
                q = self._books[venue].get(symbol)
                if not q:
                    continue
                age = (now - q["ts"]) * 1000
                if age <= stale_ms:
                    quotes.append((venue, q, age))
            if len(quotes) < 2:
                continue
            buy_venue, buy = min(quotes, key=lambda x: x[1]["ask"])
            sell_venue, sell = max(quotes, key=lambda x: x[1]["bid"])
            if buy_venue == sell_venue:
                alternatives = [(v, q, age) for v, q, age in quotes if v != buy_venue]
                if not alternatives:
                    continue
                sell_venue, sell = max(alternatives, key=lambda x: x[1]["bid"])
            if sell["bid"] <= buy["ask"]:
                gross_bps = (sell["bid"] / buy["ask"] - 1.0) * 10000.0
            else:
                gross_bps = (sell["bid"] / buy["ask"] - 1.0) * 10000.0
            net_bps = gross_bps - fee_bps * 2.0
            executable_notional = min(buy["ask"] * buy["ask_qty"], sell["bid"] * sell["bid_qty"])
            gate = "PASS" if net_bps >= min_edge_bps and executable_notional >= min_notional else "EDGE_OR_LIQUIDITY"
            rows.append({
                "symbol": symbol, "buy_venue": buy_venue, "sell_venue": sell_venue,
                "gross_bps": round(gross_bps, 4), "net_bps": round(net_bps, 4),
                "buy_ask": buy["ask"], "sell_bid": sell["bid"],
                "buy_qty": buy["ask_qty"], "sell_qty": sell["bid_qty"],
                "executable_notional_usdt": round(executable_notional, 8),
                "buy_age_ms": round((now - buy["ts"]) * 1000, 1),
                "sell_age_ms": round((now - sell["ts"]) * 1000, 1),
                "gate": gate, "execution_ready": False,
                "execution_reason": "external order adapters and authenticated two-leg fill coordination are disabled",
            })
        rows.sort(key=lambda r: r["net_bps"], reverse=True)
        self.state["cross_exchange_opportunities"] = rows[:20]
        if rows and rows[0]["gate"] == "PASS":
            self.event("CROSS_OPPORTUNITY", f"{rows[0]['symbol']} {rows[0]['buy_venue']}->{rows[0]['sell_venue']} net_bps={rows[0]['net_bps']}")

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
                            for symbol, bid, ask, bid_qty, ask_qty in parser(data):
                                self._record_feed(venue, symbol, bid, ask, bid_qty, ask_qty)
                        except Exception as exc:
                            self.external_feeds.setdefault(venue, {})["parse_errors"] = int(self.external_feeds.setdefault(venue, {}).get("parse_errors", 0)) + 1
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
        args = [{"channel": "bbo-tbt", "instId": _okx_id(s)} for s in self.symbols]
        return [{"op": "subscribe", "args": args}]

    def _okx_parser(self, msg):
        if msg.get("arg", {}).get("channel") == "bbo-tbt":
            for d in msg.get("data", []):
                inst = d.get("instId", "")
                bids, asks = d.get("bids"), d.get("asks")
                if bids and asks:
                    yield _normalize_usd_symbol(inst), float(bids[0][0]), float(asks[0][0]), float(bids[0][1]), float(asks[0][1])

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
