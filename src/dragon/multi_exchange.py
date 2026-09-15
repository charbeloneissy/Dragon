from __future__ import annotations

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


def _http_json(url: str, timeout: float = 10.0):
    req = Request(url, headers={"User-Agent": "Dragon/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class MultiExchangeFeeds:
    """Observation-only cross-venue scanner with venue-aware symbols and perpetual reconnects."""

    def __init__(self, state, lock, event: Callable | None = None, symbols=None):
        self.state, self.lock, self.event = state, lock, event
        requested = symbols or DEFAULT_SYMBOLS
        override = os.getenv("CROSS_SYMBOLS", "").strip()
        if override:
            requested = [x.strip().upper() for x in override.split(",") if x.strip()]
        self.symbols = list(dict.fromkeys(requested))[:1000]
        self.venue_symbols = {v: set() for v in VENUES}
        self._books = {v: {} for v in VENUES}
        self._coinbase_books = {}
        self._update_times = {v: deque(maxlen=100) for v in VENUES}
        self._last_update = {v: 0.0 for v in VENUES}
        self._reconnects = {v: 0 for v in VENUES}
        self._calc_count = 0
        self._calc_started_mono = time.monotonic()

    def _emit(self, message):
        if self.event:
            try: self.event(message)
            except Exception: pass

    async def _discover(self):
        try:
            b = await asyncio.to_thread(_http_json, "https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000")
            self.venue_symbols["BYBIT"] = {x.get("symbol", "").upper() for x in (b.get("result", {}).get("list", [])) if x.get("quoteCoin") == "USDT" and x.get("status") == "Trading"}
        except Exception as e:
            self._emit(f"EXT_SYMBOL_ERROR | BYBIT | {e}")
        try:
            o = await asyncio.to_thread(_http_json, "https://www.okx.com/api/v5/public/instruments?instType=SPOT")
            self.venue_symbols["OKX"] = {x.get("baseCcy", "").upper() + "USDT" for x in (o.get("data", [])) if x.get("quoteCcy") == "USDT" and x.get("state") == "live"}
        except Exception as e:
            self._emit(f"EXT_SYMBOL_ERROR | OKX | {e}")
        try:
            c = await asyncio.to_thread(_http_json, "https://api.exchange.coinbase.com/products")
            self.venue_symbols["COINBASE"] = {x.get("base_currency", "").upper() + "USDT" for x in c if x.get("quote_currency") == "USD" and x.get("status") == "online"}
        except Exception as e:
            self._emit(f"EXT_SYMBOL_ERROR | COINBASE | {e}")
        for venue in VENUES:
            self.venue_symbols[venue] &= set(self.symbols)
            self._emit(f"EXT_SYMBOLS | {venue} supported={len(self.venue_symbols[venue])}")

    def _record_feed(self, venue, symbol, bid, ask, bid_qty, ask_qty):
        try: bid, ask, bid_qty, ask_qty = map(float, (bid, ask, bid_qty, ask_qty))
        except (TypeError, ValueError): return
        if min(bid, ask, bid_qty, ask_qty) <= 0 or ask < bid: return
        now = time.monotonic()
        self._books[venue][symbol] = {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "ts": now}
        times = self._update_times[venue]; times.append(now)
        ups = (len(times)-1) / max(times[-1]-times[0], 1e-6) if len(times) > 1 else 0.0
        with self.lock:
            feeds = self.state.setdefault("external", {}).setdefault("feeds", {})
            feeds.setdefault(venue, {})[symbol] = {"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "quote_age_ms": 0.0, "updates_per_sec": ups, "last_update": time.time()}

    def _record_control(self, venue, msg):
        if venue == "BYBIT" and msg.get("op") == "subscribe":
            self._emit(f"EXT_SUB_{'OK' if msg.get('success') else 'ERROR'} | BYBIT | {msg.get('ret_msg', '')}")

    def _bybit_messages(self, symbols):
        ordered = list(dict.fromkeys(["BTCUSDT"] + list(symbols)))
        return [{"op":"subscribe","args":[f"orderbook.1.{s}" for s in ordered[i:i+10]]} for i in range(0,len(ordered),10)]

    def _okx_messages(self, symbols):
        return [{"op":"subscribe","args":[{"channel":"bbo-tbt","instId":f"{s[:-4]}-USDT"} for s in symbols[i:i+100]]} for i in range(0,len(symbols),100)]

    def _coinbase_messages(self, symbols):
        products = [f"{s[:-4]}-USD" for s in symbols]
        return [{"type":"subscribe","product_ids":products[i:i+100],"channel":"level2"} for i in range(0,len(products),100)]

    def _bybit_parser(self, msg):
        if not msg.get("topic", "").startswith("orderbook.1."): return
        d = msg.get("data") or {}; s = d.get("s"); b, a = d.get("b") or [], d.get("a") or []
        if s and b and a: yield s.upper(), float(b[0][0]), float(a[0][0]), float(b[0][1]), float(a[0][1])

    def _okx_parser(self, msg):
        if msg.get("arg", {}).get("channel") != "bbo-tbt": return
        for d in msg.get("data") or []:
            inst = d.get("instId", "")
            if not inst.endswith("-USDT"): continue
            b, a = d.get("bids") or [], d.get("asks") or []
            if b and a: yield inst[:-5] + "USDT", float(b[0][0]), float(a[0][0]), float(b[0][1]), float(a[0][1])

    def _coinbase_parser(self, msg):
        if msg.get("channel") != "l2_data": return
        for ev in msg.get("events") or []:
            for u in ev.get("updates") or []:
                p = u.get("product_id", "")
                if not p.endswith("-USD"): continue
                price, qty = float(u.get("price", 0) or 0), float(u.get("new_quantity", 0) or 0)
                if price <= 0: continue
                s = p[:-4] + "USDT"; side = "bid" if u.get("side") == "bid" else "ask"
                levels = self._coinbase_books.setdefault(s, {"bid":{},"ask":{}}); levels[side][price] = qty
                if qty <= 0: levels[side].pop(price, None)
                if levels["bid"] and levels["ask"]:
                    bp, ap = max(levels["bid"]), min(levels["ask"])
                    if ap >= bp: yield s, bp, ap, levels["bid"][bp], levels["ask"][ap]

    async def _run_venue(self, venue, symbols, connection_no):
        urls={"BYBIT":"wss://stream.bybit.com/v5/public/spot","OKX":"wss://ws.okx.com:8443/ws/v5/public","COINBASE":"wss://advanced-trade-ws.coinbase.com"}
        builders={"BYBIT":self._bybit_messages,"OKX":self._okx_messages,"COINBASE":self._coinbase_messages}
        parsers={"BYBIT":self._bybit_parser,"OKX":self._okx_parser,"COINBASE":self._coinbase_parser}
        delay=WS_BACKOFF_MIN
        while True:
            try:
                self._emit(f"EXT_WS | {venue} connection-{connection_no} connecting")
                async with websockets.connect(urls[venue], ping_interval=WS_PING_INTERVAL, ping_timeout=WS_PING_TIMEOUT, close_timeout=WS_CLOSE_TIMEOUT, open_timeout=WS_OPEN_TIMEOUT, max_size=WS_MAX_SIZE, max_queue=WS_MAX_QUEUE, compression=None) as ws:
                    messages = builders[venue](symbols)
                    for msg in messages:
                        await ws.send(json.dumps(msg)); await asyncio.sleep(WS_SUBSCRIBE_DELAY)
                    self._emit(f"EXT_WS | {venue} connection-{connection_no} connected subscriptions={len(messages)} symbols={len(symbols)}")
                    delay=WS_BACKOFF_MIN
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=WS_STALE_SECONDS)
                        if raw is None: raise ConnectionError("websocket closed")
                        msg=json.loads(raw); self._record_control(venue,msg)
                        for item in parsers[venue](msg) or (): self._record_feed(venue,*item)
            except asyncio.CancelledError: raise
            except Exception as exc:
                self._reconnects[venue]+=1
                self._emit(f"EXT_WS_ERROR | {venue} connection-{connection_no} | {type(exc).__name__}: {exc} | reconnect #{self._reconnects[venue]}")
                await asyncio.sleep(min(delay,WS_BACKOFF_MAX)*(0.8+random.random()*0.4)); delay=min(delay*2,WS_BACKOFF_MAX)

    def _update_opportunities(self):
        stale_ms=float(os.getenv("CROSS_STALE_MS","1500")); max_sync=float(os.getenv("CROSS_MAX_SYNC_SKEW_MS","500"))
        min_net=float(os.getenv("CROSS_MIN_NET_EDGE_BPS","0.10")); slippage=float(os.getenv("CROSS_SLIPPAGE_BPS","0.50")); latency_rate=float(os.getenv("CROSS_LATENCY_BPS_PER_MS","0.005")); evaluation=float(os.getenv("CROSS_EVALUATION_NOTIONAL_USDT","5"))
        fee_bps={"BYBIT":float(os.getenv("CROSS_FEE_BPS_BYBIT","10")),"OKX":float(os.getenv("CROSS_FEE_BPS_OKX","10")),"COINBASE":float(os.getenv("CROSS_FEE_BPS_COINBASE","10"))}
        now=time.monotonic(); rows=[]; start=now
        for symbol in self.symbols:
            for buy in VENUES:
                if symbol not in self.venue_symbols[buy]: continue
                b=self._books[buy].get(symbol)
                if not b: continue
                for sell in VENUES:
                    if sell==buy or symbol not in self.venue_symbols[sell]: continue
                    s=self._books[sell].get(symbol)
                    if not s: continue
                    ba=(now-b["ts"])*1000; sa=(now-s["ts"])*1000; skew=abs(b["ts"]-s["ts"])*1000
                    if max(ba,sa)>stale_ms or skew>max_sync: continue
                    gross=(s["bid"]/b["ask"]-1)*10000; fees=fee_bps[buy]+fee_bps[sell]; latency=max(ba,sa)*latency_rate; net=gross-fees-slippage-latency
                    notional=max(0.0,min(evaluation,b["ask"]*b["ask_qty"],s["bid"]*s["bid_qty"]))
                    rows.append({"symbol":symbol,"buy_venue":buy,"sell_venue":sell,"gross_edge_bps":gross,"fee_bps":fees,"slippage_bps":slippage,"latency_penalty_bps":latency,"net_edge_bps":net,"executable_notional_usdt":notional,"expected_pnl_usdt":notional*net/10000,"gate":"PASS" if net>=min_net and notional>0 else "EDGE_OR_LIQUIDITY"})
        rows.sort(key=lambda x:x["net_edge_bps"],reverse=True)
        elapsed=(time.monotonic()-start)*1000; self._calc_count+=1
        with self.lock:
            ext=self.state.setdefault("external",{}); ext["opportunities"]=rows[:50]; ext["calculation_ms"]=elapsed; ext["calculations_per_sec"]=self._calc_count/max(time.monotonic()-self._calc_started_mono,1e-6); ext["reconnects"]=dict(self._reconnects); ext["supported_symbols"]={v:len(self.venue_symbols[v]) for v in VENUES}; ext["external_scan_status"]="RUNNING"

    async def _calculator(self):
        while True:
            self._update_opportunities(); await asyncio.sleep(max(0.05,float(os.getenv("CROSS_RECALC_MIN_MS","100"))/1000))

    async def run(self):
        await self._discover()
        tasks=[asyncio.create_task(self._calculator())]
        for venue in VENUES:
            venue_symbols=sorted(self.venue_symbols[venue])
            for i in range(0,len(venue_symbols),WS_SYMBOLS_PER_CONNECTION):
                shard=venue_symbols[i:i+WS_SYMBOLS_PER_CONNECTION]
                if shard: tasks.append(asyncio.create_task(self._run_venue(venue,shard,i//WS_SYMBOLS_PER_CONNECTION+1)))
        await asyncio.gather(*tasks)
