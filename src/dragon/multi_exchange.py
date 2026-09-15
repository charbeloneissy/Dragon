"""Public multi-exchange market-data feeds and net-edge discovery.

Cross-exchange execution is intentionally gated: this module produces ranked,
size-aware opportunities and execution-readiness telemetry. It never submits
orders to external venues.
"""

import asyncio
import json
import os
import time
from collections import defaultdict

import websockets

DEFAULT_SYMBOLS = """BTCUSDT,ETHUSDT,BNBUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,ADAUSDT,AVAXUSDT,LINKUSDT,
DOTUSDT,TRXUSDT,LTCUSDT,BCHUSDT,UNIUSDT,NEARUSDT,ATOMUSDT,APTUSDT,ARBUSDT,
OPUSDT,FILUSDT,ETCUSDT,ICPUSDT,INJUSDT,SUIUSDT,SEIUSDT,TONUSDT,HBARUSDT,
AAVEUSDT,PEPEUSDT,SHIBUSDT,RENDERUSDT,FETUSDT,TAOUSDT,ENAUSDT,WIFUSDT,
JUPUSDT,STXUSDT,IMXUSDT,MKRUSDT,RUNEUSDT,GRTUSDT,ALGOUSDT,XLMUSDT,
VETUSDT,EOSUSDT,XTZUSDT,THETAUSDT,CRVUSDT,LDOUSDT""".replace("\n", "")

# Conservative default taker-fee assumptions for discovery. Override in Render
# once the actual account fee tiers are known.
DEFAULT_FEES_BPS = {"BYBIT": 10.0, "OKX": 10.0, "COINBASE": 40.0, "BINANCE": 10.0}


def _symbols() -> list[str]:
    return list(dict.fromkeys(s.strip().upper() for s in DEFAULT_SYMBOLS.split(",") if s.strip()))


def _okx_id(symbol: str) -> str:
    return f"{symbol[:-4]}-USDT" if symbol.endswith("USDT") else symbol


def _coinbase_id(symbol: str) -> str:
    return f"{symbol[:-4]}-USD" if symbol.endswith("USDT") else symbol


def _normalize_usd_symbol(symbol: str) -> str:
    symbol = symbol.replace("-", "").upper()
    if symbol.endswith("USD") and not symbol.endswith("USDT"):
        return symbol + "T"
    return symbol


def _fee_bps(venue: str) -> float:
    key = f"CROSS_{venue}_FEE_BPS"
    try:
        return max(0.0, float(os.getenv(key, DEFAULT_FEES_BPS[venue])))
    except (TypeError, ValueError):
        return DEFAULT_FEES_BPS[venue]


class MultiExchangeFeeds:
    """Resilient public Bybit/OKX/Coinbase BBO collectors."""

    def __init__(self, state, lock, event):
        self.state = state
        self.lock = lock
        self.event = event
        self.books: dict[str, dict[str, dict[str, float]]] = defaultdict(dict)
        self._coinbase_books: dict[str, dict[str, dict[float, float]]] = defaultdict(lambda: {"bid": {}, "offer": {}})
        self._tasks: list[asyncio.Task] = []
        self.symbols = _symbols()
        self.enabled = os.getenv("CROSS_EXCHANGE_ENABLED", "true").lower() == "true"
        self.stale_ms = max(250, int(os.getenv("CROSS_STALE_MS", "1500")))
        self.min_net_bps = float(os.getenv("CROSS_MIN_NET_EDGE_BPS", "1.0"))
        self.max_notional = max(1.0, float(os.getenv("CROSS_MAX_NOTIONAL_USDT", "25")))

    def _update(self, venue: str, symbol: str, bid: float, ask: float, bid_qty=0.0, ask_qty=0.0):
        symbol = _normalize_usd_symbol(symbol)
        if bid <= 0 or ask <= 0 or ask < bid:
            return
        now = time.time()
        self.books[venue][symbol] = {
            "bid": bid, "ask": ask,
            "bid_qty": float(bid_qty or 0), "ask_qty": float(ask_qty or 0), "ts": now,
        }
        with self.lock:
            feeds = self.state.setdefault("external_feeds", {})
            self.state["external_feed_updates"] = int(self.state.get("external_feed_updates", 0)) + 1
            feed = feeds.setdefault(venue, {"status": "starting", "updates": 0, "last_update": None})
            feed["updates"] = int(feed.get("updates", 0)) + 1
            feed["last_update"] = now
            feed["status"] = "connected"

            fresh = [
                (v, q) for v, books in self.books.items()
                if (q := books.get(symbol)) and (now - q["ts"]) * 1000 <= self.stale_ms
            ]
            if len(fresh) < 2:
                return

            buy_v, buy_q = min(fresh, key=lambda x: x[1]["ask"])
            sell_v, sell_q = max(fresh, key=lambda x: x[1]["bid"])
            if buy_v == sell_v:
                return

            # Size is bounded by both displayed top-of-book quantities and the
            # configured cap. Zero quantities are treated as non-executable.
            buy_qty = max(0.0, buy_q.get("ask_qty", 0.0))
            sell_qty = max(0.0, sell_q.get("bid_qty", 0.0))
            size_base = min(buy_qty, sell_qty, self.max_notional / buy_q["ask"])
            if size_base <= 0:
                return

            buy_notional = size_base * buy_q["ask"]
            sell_notional = size_base * sell_q["bid"]
            gross_bps = (sell_q["bid"] / buy_q["ask"] - 1.0) * 10000
            # Both legs pay taker fees. No transfer, withdrawal, latency, or
            # inventory benefit is assumed, so those remain execution risks.
            fee_bps = _fee_bps(buy_v) + _fee_bps(sell_v)
            net_bps = gross_bps - fee_bps
            net_pnl = buy_notional * (net_bps / 10000.0)
            age_ms = max((now - buy_q["ts"]) * 1000, (now - sell_q["ts"]) * 1000)

            rows = [x for x in self.state.get("cross_exchange_opportunities", []) if x.get("symbol") != symbol]
            rows.append({
                "symbol": symbol, "buy": buy_v, "sell": sell_v,
                "gross_bps": round(gross_bps, 3), "fee_bps": round(fee_bps, 3),
                "net_bps": round(net_bps, 3), "notional_usdt": round(buy_notional, 6),
                "estimated_net_pnl_usdt": round(net_pnl, 6),
                "buy_ask": buy_q["ask"], "sell_bid": sell_q["bid"],
                "buy_qty": buy_qty, "sell_qty": sell_qty,
                "age_ms": round(age_ms, 1), "executable_bbo": net_bps >= self.min_net_bps,
                "execution_ready": False, "ts": now,
            })
            rows.sort(key=lambda x: x["net_bps"], reverse=True)
            self.state["cross_exchange_opportunities"] = rows[:20]
            self.state["cross_exchange_execution"] = {
                "enabled": self.enabled,
                "market_data_ready": True,
                "execution_ready": False,
                "reason": "External order adapters and authenticated two-leg fill coordination are not enabled",
                "last_candidate": rows[0] if rows else None,
            }
            if net_bps >= self.min_net_bps:
                self.event(
                    "CROSS_OPPORTUNITY",
                    f"{symbol} {buy_v}->{sell_v} net={net_bps:.3f}bps size={buy_notional:.4f}USDT",
                    gross_bps=round(gross_bps, 3), fee_bps=round(fee_bps, 3),
                    net_bps=round(net_bps, 3), notional_usdt=round(buy_notional, 6),
                )

    async def _run(self, venue, url, subscribe_messages, parser):
        delay = 1.0
        while True:
            try:
                with self.lock:
                    self.state.setdefault("external_feeds", {}).setdefault(
                        venue, {"status": "starting", "updates": 0, "last_update": None}
                    )["status"] = "connecting"
                async with websockets.connect(
                    url, ping_interval=20, ping_timeout=20, close_timeout=5,
                    open_timeout=15, max_size=2**24, compression=None
                ) as ws:
                    for message in subscribe_messages:
                        await ws.send(json.dumps(message))
                    delay = 1.0
                    with self.lock:
                        self.state["external_feeds"][venue]["status"] = "connected"
                    self.event("EXT_WS", f"{venue} public market feed connected", symbols=len(self.symbols))
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                        try:
                            for symbol, bid, ask, bq, aq in parser(json.loads(raw)):
                                self._update(venue, symbol, bid, ask, bq, aq)
                        except Exception as exc:
                            self.event("EXT_WS_PARSE", f"{venue}: {exc}")
            except asyncio.CancelledError:
                with self.lock:
                    if venue in self.state.get("external_feeds", {}):
                        self.state["external_feeds"][venue]["status"] = "stopped"
                raise
            except Exception as exc:
                with self.lock:
                    feed = self.state.setdefault("external_feeds", {}).setdefault(venue, {})
                    feed["status"] = "reconnecting"
                    feed["last_error"] = str(exc)
                    feed["reconnects"] = int(feed.get("reconnects", 0)) + 1
                self.event("EXT_WS_ERROR", f"{venue}: {exc}")
                await asyncio.sleep(delay)
                delay = min(60.0, delay * 2)

    def _bybit_messages(self):
        return [
            {"op": "subscribe", "args": [f"orderbook.1.{s}" for s in self.symbols[i:i + 10]]}
            for i in range(0, len(self.symbols), 10)
        ]

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
            product = event.get("product_id", "")
            symbol = _normalize_usd_symbol(product)
            book = self._coinbase_books[symbol]
            for update in event.get("updates", []):
                side = update.get("side")
                if side not in ("bid", "offer"):
                    continue
                try:
                    price = float(update.get("price_level", 0))
                    qty = float(update.get("new_quantity", 0))
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                if qty <= 0:
                    book[side].pop(price, None)
                else:
                    book[side][price] = qty
            if not book["bid"] or not book["offer"]:
                continue
            bid = max(book["bid"])
            ask = min(book["offer"])
            if ask > bid:
                yield symbol, bid, ask, book["bid"][bid], book["offer"][ask]

    async def run(self):
        if not self.enabled:
            with self.lock:
                self.state["cross_exchange_execution"] = {
                    "enabled": False, "market_data_ready": False,
                    "execution_ready": False, "reason": "CROSS_EXCHANGE_ENABLED=false",
                }
            return
        self._tasks = [
            asyncio.create_task(self._run("BYBIT", "wss://stream.bybit.com/v5/public/spot", self._bybit_messages(), self._bybit_parser)),
            asyncio.create_task(self._run("OKX", "wss://ws.okx.com:8443/ws/v5/public", self._okx_messages(), self._okx_parser)),
            asyncio.create_task(self._run("COINBASE", "wss://advanced-trade-ws.coinbase.com", self._coinbase_messages(), self._coinbase_parser)),
        ]
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
