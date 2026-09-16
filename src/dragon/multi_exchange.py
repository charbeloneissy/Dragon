from __future__ import annotations
import asyncio,json,os,random,time
from collections import deque
from typing import Callable
from urllib.request import Request,urlopen
import websockets
VENUES=("BYBIT","OKX","COINBASE")
DEFAULT_SYMBOLS=["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","AVAXUSDT","LINKUSDT","DOTUSDT"]
WS_BACKOFF_MIN=1.0
WS_BACKOFF_MAX=60.0
WS_STALE_SECONDS=float(os.getenv("CROSS_WS_STALE_SECONDS","45"))
WS_PING_INTERVAL=float(os.getenv("CROSS_WS_PING_INTERVAL","20"))
WS_PING_TIMEOUT=float(os.getenv("CROSS_WS_PING_TIMEOUT","15"))
WS_OPEN_TIMEOUT=float(os.getenv("CROSS_WS_OPEN_TIMEOUT","20"))
WS_CLOSE_TIMEOUT=float(os.getenv("CROSS_WS_CLOSE_TIMEOUT","5"))
WS_MAX_QUEUE=int(os.getenv("CROSS_WS_MAX_QUEUE","8192"))
WS_MAX_SIZE=int(os.getenv("CROSS_WS_MAX_SIZE",str(2**24)))
WS_SYMBOLS_PER_CONNECTION=max(50,int(os.getenv("CROSS_WS_SYMBOLS_PER_CONNECTION","100")))
WS_SUBSCRIBE_DELAY=max(0.0,float(os.getenv("CROSS_WS_SUBSCRIBE_DELAY","0.15")))
WS_RECONNECT_JITTER=float(os.getenv("CROSS_WS_RECONNECT_JITTER","0.25"))

def _http_json(url,timeout=10.0):
    req=Request(url,headers={"User-Agent":"Dragon/1.0"})
    with urlopen(req,timeout=timeout) as r:return json.loads(r.read().decode("utf-8"))
def _clamp(v,lo,hi):return max(lo,min(hi,v))
def _pct(values,q,default=0.0):
    if not values:return default
    x=sorted(values);p=(len(x)-1)*q;i=int(p);j=min(i+1,len(x)-1)
    return x[i]+(x[j]-x[i])*(p-i)

class MultiExchangeFeeds:
    """Observation-only cross-venue scanner with isolated, self-healing connection hubs."""
    def __init__(self,state,lock,event:Callable|None=None,symbols=None):
        self.state,self.lock,self.event=state,lock,event
        requested=symbols or DEFAULT_SYMBOLS
        override=os.getenv("CROSS_SYMBOLS","").strip()
        if override:requested=[x.strip().upper() for x in override.split(",") if x.strip()]
        self.symbols=list(dict.fromkeys(requested))[:1000]
        self.venue_symbols={v:set() for v in VENUES};self._books={v:{} for v in VENUES};self._coinbase_books={}
        self._update_times={v:deque(maxlen=100) for v in VENUES};self._reconnects={v:0 for v in VENUES};self._last_updates={v:0.0 for v in VENUES};self._active_connections={v:0 for v in VENUES}
        self._calc_count=0;self._calc_started_mono=time.monotonic()
        self._gross=deque(maxlen=500);self._ages=deque(maxlen=500);self._depth=deque(maxlen=500)
        self._adaptive={"min_net_edge_bps":float(os.getenv("CROSS_MIN_NET_EDGE_BPS","0.10")),"slippage_bps":float(os.getenv("CROSS_SLIPPAGE_BPS","0.50")),"max_sync_skew_ms":float(os.getenv("CROSS_MAX_SYNC_SKEW_MS","500"))}
    def _emit(self,m):
        if not self.event:return
        try:
            text=str(m);parts=text.split(" | ",1)
            if len(parts)==2:self.event(parts[0],parts[1])
            else:self.event("EXT",text)
        except Exception:pass
    async def _discover(self):
        try:
            b=await asyncio.to_thread(_http_json,"https://api.bybit.com/v5/market/instruments-info?category=spot&limit=1000")
            self.venue_symbols["BYBIT"]={x.get("symbol","").upper() for x in b.get("result",{}).get("list",[]) if x.get("quoteCoin")=="USDT" and x.get("status")=="Trading"}
        except Exception as e:self._emit(f"EXT_SYMBOL_ERROR | BYBIT | {e}")
        try:
            o=await asyncio.to_thread(_http_json,"https://www.okx.com/api/v5/public/instruments?instType=SPOT")
            self.venue_symbols["OKX"]={x.get("baseCcy","").upper()+"USDT" for x in o.get("data",[]) if x.get("quoteCcy")=="USDT" and x.get("state")=="live"}
        except Exception as e:self._emit(f"EXT_SYMBOL_ERROR | OKX | {e}")
        try:
            c=await asyncio.to_thread(_http_json,"https://api.exchange.coinbase.com/products")
            self.venue_symbols["COINBASE"]={x.get("base_currency","").upper()+"USDT" for x in c if x.get("quote_currency")=="USD" and x.get("status")=="online"}
        except Exception as e:self._emit(f"EXT_SYMBOL_ERROR | COINBASE | {e}")
        for v in VENUES:
            self.venue_symbols[v]&=set(self.symbols);self._emit(f"EXT_SYMBOLS | {v} supported={len(self.venue_symbols[v])}")
    def _record_feed(self,v,s,bid,ask,bq,aq):
        try:bid,ask,bq,aq=map(float,(bid,ask,bq,aq))
        except (TypeError,ValueError):return
        if min(bid,ask,bq,aq)<=0 or ask<bid:return
        now=time.monotonic();self._books[v][s]={"bid":bid,"ask":ask,"bid_qty":bq,"ask_qty":aq,"ts":now};self._last_updates[v]=now
        t=self._update_times[v];t.append(now);ups=(len(t)-1)/max(t[-1]-t[0],1e-6) if len(t)>1 else 0.0
        with self.lock:self.state.setdefault("external",{}).setdefault("feeds",{}).setdefault(v,{})[s]={"bid":bid,"ask":ask,"bid_qty":bq,"ask_qty":aq,"quote_age_ms":0.0,"updates_per_sec":ups,"last_update":time.time()}
    def _record_control(self,v,msg):
        if v=="BYBIT" and msg.get("op")=="subscribe":self._emit(f"EXT_SUB_{'OK' if msg.get('success') else 'ERROR'} | BYBIT | {msg.get('ret_msg','')}")
    def _bybit_messages(self,s):
        x=list(dict.fromkeys(s));return [{"op":"subscribe","args":[f"orderbook.1.{z}" for z in x[i:i+10]]} for i in range(0,len(x),10)]
    def _okx_messages(self,s):return [{"op":"subscribe","args":[{"channel":"bbo-tbt","instId":f"{z[:-4]}-USDT"} for z in s[i:i+100]]} for i in range(0,len(s),100)]
    def _coinbase_messages(self,s):
        p=[f"{z[:-4]}-USD" for z in s];return [{"type":"subscribe","product_ids":p[i:i+100],"channel":"level2"} for i in range(0,len(p),100)]
    def _bybit_parser(self,m):
        if not m.get("topic","").startswith("orderbook.1."):return
        d=m.get("data") or {};s=d.get("s");b=d.get("b") or [];a=d.get("a") or []
        if s and b and a:yield s.upper(),float(b[0][0]),float(a[0][0]),float(b[0][1]),float(a[0][1])
    def _okx_parser(self,m):
        if m.get("arg",{}).get("channel")!="bbo-tbt":return
        for d in m.get("data") or []:
            z=d.get("instId","");b=d.get("bids") or [];a=d.get("asks") or []
            if z.endswith("-USDT") and b and a:yield z[:-5]+"USDT",float(b[0][0]),float(a[0][0]),float(b[0][1]),float(a[0][1])
    def _coinbase_parser(self,m):
        if m.get("channel")!="l2_data":return
        for ev in m.get("events") or []:
            for u in ev.get("updates") or []:
                p=u.get("product_id","")
                if not p.endswith("-USD"):continue
                price,qty=float(u.get("price",0) or 0),float(u.get("new_quantity",0) or 0)
                if price<=0:continue
                s=p[:-4]+"USDT";side="bid" if u.get("side")=="bid" else "ask";lv=self._coinbase_books.setdefault(s,{"bid":{},"ask":{}});lv[side][price]=qty
                if qty<=0:lv[side].pop(price,None)
                if lv["bid"] and lv["ask"]:
                    bp,ap=max(lv["bid"]),min(lv["ask"])
                    if ap>=bp:yield s,bp,ap,lv["bid"][bp],lv["ask"][ap]
    def _adapt(self):
        if len(self._gross)<20:return
        base=float(os.getenv("CROSS_MIN_NET_EDGE_BPS","0.10"));slip=float(os.getenv("CROSS_SLIPPAGE_BPS","0.50"));sync=float(os.getenv("CROSS_MAX_SYNC_SKEW_MS","500"));evaluation=float(os.getenv("CROSS_EVALUATION_NOTIONAL_USDT","5"))
        p25=_pct(self._gross,.25);age=_pct(self._ages,.5);depth=_pct(self._depth,.5)
        amin=_clamp(max(base,p25*.10),.10,5.0);af=_clamp(age/1000,0,2);df=_clamp(evaluation/max(depth,evaluation),0,1)
        self._adaptive={"min_net_edge_bps":amin,"slippage_bps":_clamp(slip*(1+.75*af+.75*df),slip,3.0),"max_sync_skew_ms":_clamp(sync*(1-.25*af),100.0,sync)}
    async def _run_venue(self,v,symbols,n):
        urls={"BYBIT":"wss://stream.bybit.com/v5/public/spot","OKX":"wss://ws.okx.com:8443/ws/v5/public","COINBASE":"wss://advanced-trade-ws.coinbase.com"};builders={"BYBIT":self._bybit_messages,"OKX":self._okx_messages,"COINBASE":self._coinbase_messages};parsers={"BYBIT":self._bybit_parser,"OKX":self._okx_parser,"COINBASE":self._coinbase_parser};delay=WS_BACKOFF_MIN
        while True:
            try:
                self._emit(f"EXT_WS | {v} connection-{n} connecting")
                async with websockets.connect(urls[v],ping_interval=WS_PING_INTERVAL,ping_timeout=WS_PING_TIMEOUT,close_timeout=WS_CLOSE_TIMEOUT,open_timeout=WS_OPEN_TIMEOUT,max_size=WS_MAX_SIZE,max_queue=WS_MAX_QUEUE,compression=None) as ws:
                    self._active_connections[v]+=1
                    try:
                        msgs=builders[v](symbols)
                        for m in msgs:await ws.send(json.dumps(m));await asyncio.sleep(WS_SUBSCRIBE_DELAY)
                        self._emit(f"EXT_WS | {v} connection-{n} connected subscriptions={len(msgs)} symbols={len(symbols)}");delay=WS_BACKOFF_MIN
                        while True:
                            raw=await asyncio.wait_for(ws.recv(),timeout=WS_STALE_SECONDS)
                            if raw is None:raise ConnectionError("websocket closed")
                            m=json.loads(raw);self._record_control(v,m)
                            for item in parsers[v](m) or ():self._record_feed(v,*item)
                    finally:
                        self._active_connections[v]=max(0,self._active_connections[v]-1)
            except asyncio.CancelledError:raise
            except Exception as e:
                self._reconnects[v]+=1;self._emit(f"EXT_WS_ERROR | {v} connection-{n} | {type(e).__name__}: {e} | reconnect #{self._reconnects[v]}")
                wait=min(WS_BACKOFF_MAX,delay)*(1+random.uniform(-WS_RECONNECT_JITTER,WS_RECONNECT_JITTER));self._emit(f"EXT_WS_RETRY | {v} connection-{n} | retry in {wait:.1f}s")
                await asyncio.sleep(max(0.25,wait));delay=min(delay*2,WS_BACKOFF_MAX)
    def _update_hub_health(self):
        now=time.monotonic();stale=float(os.getenv("CROSS_WS_STALE_SECONDS",str(WS_STALE_SECONDS)))
        with self.lock:
            e=self.state.setdefault("external",{});e["hub_health"]={v:{"connections":self._active_connections[v],"last_update":time.time() if self._last_updates[v] else None,"age_ms":(now-self._last_updates[v])*1000 if self._last_updates[v] else None,"reconnects":self._reconnects[v],"healthy":bool(self._last_updates[v] and now-self._last_updates[v]<=stale)} for v in VENUES}
    def _update_opportunities(self):
        stale=float(os.getenv("CROSS_STALE_MS","1500"));lat_rate=float(os.getenv("CROSS_LATENCY_BPS_PER_MS","0.005"));evaluation=float(os.getenv("CROSS_EVALUATION_NOTIONAL_USDT","5"));fees={v:float(os.getenv(f"CROSS_FEE_BPS_{v}","10")) for v in VENUES};self._adapt();now=time.monotonic();rows=[];started=now
        for s in self.symbols:
            for buy in VENUES:
                if s not in self.venue_symbols[buy]:continue
                b=self._books[buy].get(s)
                if not b:continue
                for sell in VENUES:
                    if sell==buy or s not in self.venue_symbols[sell]:continue
                    x=self._books[sell].get(s)
                    if not x:continue
                    ba,sa=(now-b["ts"])*1000,(now-x["ts"])*1000;skew=abs(b["ts"]-x["ts"])*1000
                    if max(ba,sa)>stale or skew>self._adaptive["max_sync_skew_ms"]:continue
                    gross=(x["bid"]/b["ask"]-1)*10000;notional=max(0,min(evaluation,b["ask"]*b["ask_qty"],x["bid"]*x["bid_qty"]))
                    if notional<=0:continue
                    self._gross.append(gross);self._ages.append(max(ba,sa));self._depth.append(notional);lat=max(ba,sa)*lat_rate;fee=fees[buy]+fees[sell];net=gross-fee-self._adaptive["slippage_bps"]-lat
                    rows.append({"symbol":s,"buy_venue":buy,"sell_venue":sell,"gross_edge_bps":gross,"fee_bps":fee,"slippage_bps":self._adaptive["slippage_bps"],"latency_penalty_bps":lat,"net_edge_bps":net,"executable_notional_usdt":notional,"expected_pnl_usdt":notional*net/10000,"gate":"PASS" if net>=self._adaptive["min_net_edge_bps"] else "EDGE_OR_LIQUIDITY"})
        rows.sort(key=lambda r:r["net_edge_bps"],reverse=True);elapsed=(time.monotonic()-started)*1000;self._calc_count+=1
        with self.lock:
            e=self.state.setdefault("external",{});e["opportunities"]=rows[:50];e["calculation_ms"]=elapsed;e["calculations_per_sec"]=self._calc_count/max(time.monotonic()-self._calc_started_mono,1e-6);e["reconnects"]=dict(self._reconnects);e["supported_symbols"]={v:len(self.venue_symbols[v]) for v in VENUES};e["adaptive_params"]=dict(self._adaptive);e["adaptive_samples"]={"gross_edges":len(self._gross),"quote_age":len(self._ages),"depth":len(self._depth)};e["external_scan_status"]="RUNNING"
    async def _calculator(self):
        while True:self._update_opportunities();self._update_hub_health();await asyncio.sleep(max(.05,float(os.getenv("CROSS_RECALC_MIN_MS","100"))/1000))
    async def run(self):
        await self._discover()
        tasks=[]
        for v in VENUES:
            syms=sorted(self.venue_symbols[v]);
            for i in range(0,len(syms),WS_SYMBOLS_PER_CONNECTION):tasks.append(asyncio.create_task(self._run_venue(v,syms[i:i+WS_SYMBOLS_PER_CONNECTION],i//WS_SYMBOLS_PER_CONNECTION+1)))
        tasks.append(asyncio.create_task(self._calculator()));await asyncio.gather(*tasks)
