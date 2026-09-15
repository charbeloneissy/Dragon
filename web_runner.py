import asyncio
import json
import os
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import websockets

from src.dragon.binance import BinanceClient
from src.dragon.config import Config
from src.dragon.executor import execute_triangle
from src.dragon.risk import approved, risk_budget
from src.dragon.triangles import build_triangles, evaluate_triangle

STATE = {
    "started_at": None, "ws_connected": False, "triangles": 0, "symbols": 0,
    "opportunities": 0, "executions": 0, "execution_errors": 0,
    "risk_blocks": 0, "reconnects": 0, "last_opportunity": None,
    "last_execution": None, "last_error": None, "recent": [],
}
LOCK = __import__("threading").Lock()


def event(kind, message, **data):
    item = {"ts": time.time(), "kind": kind, "message": message, **data}
    with LOCK:
        STATE["recent"].append(item)
        STATE["recent"] = STATE["recent"][-40:]


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            payload = {
                "status": "ok", "service": "dragon", "state": STATE,
                "live": bool(STATE.get("live")), "dry_run": bool(STATE.get("dry_run")),
            }
            body = json.dumps(payload, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path in ("/dashboard", "/dashboard/"):
            body = DASHBOARD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/manifest.json":
            body = MANIFEST.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/service-worker.js":
            body = SERVICE_WORKER.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Service-Worker-Allowed", "/")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/dragon-icon.svg":
            body = DRAGON_ICON.encode()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()

    def log_message(self, *_args): return


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    print(f"HEALTH SERVER | 0.0.0.0:{port}", flush=True)
    return server


MANIFEST = r'''{
  "name": "Dragon Arbitrage Live",
  "short_name": "Dragon",
  "description": "Dragon arbitrage engine live dashboard",
  "start_url": "/dashboard",
  "scope": "/",
  "display": "standalone",
  "orientation": "portrait",
  "background_color": "#0b0d10",
  "theme_color": "#0b0d10",
  "icons": [
    {"src":"/dragon-icon.svg","sizes":"any","type":"image/svg+xml","purpose":"any maskable"}
  ]
}'''

SERVICE_WORKER = r'''const CACHE_NAME = "dragon-dashboard-v1";
const APP_SHELL = ["/dashboard", "/manifest.json", "/dragon-icon.svg"];
self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(APP_SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", event => {
  const url = new URL(event.request.url);
  if (url.pathname === "/health" || url.pathname === "/healthz") return;
  if (event.request.method !== "GET" || url.origin !== self.location.origin) return;
  event.respondWith(fetch(event.request).catch(() => caches.match(event.request)));
});'''

DRAGON_ICON = r'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512"><rect width="512" height="512" rx="112" fill="#0b0d10"/><path d="M106 364c62-18 78-69 76-122-2-60 31-113 99-139-4 32 10 48 38 64 27 16 51 40 61 75 8 29 4 60-14 88-18 28-48 50-84 61-53 17-113 10-176-27z" fill="#c9a85b"/><path d="M229 188c34-13 69-8 97 12-20 5-37 17-48 35-14-15-31-31-49-47z" fill="#0b0d10"/><circle cx="333" cy="196" r="10" fill="#c9a85b"/><path d="M385 317l42 28-55 1z" fill="#c9a85b"/></svg>'''

DASHBOARD = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="theme-color" content="#0b0d10"><meta name="mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="manifest" href="/manifest.json"><link rel="icon" href="/dragon-icon.svg"><title>Dragon Arbitrage Live</title><style>body{margin:0;background:#0b0d10;color:#eee;font-family:system-ui,Arial}main{max-width:1100px;margin:auto;padding:22px;padding-bottom:40px}h1{margin:0 0 4px}.sub{color:#8f98a3;margin-bottom:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.card{background:#14181e;border:1px solid #252b34;border-radius:14px;padding:16px}.label{color:#8f98a3;font-size:12px;text-transform:uppercase}.value{font-size:28px;font-weight:700;margin-top:5px}.good{color:#55d68a}.warn{color:#f0c75e}.bad{color:#ff6874}.feed{margin-top:16px;max-height:430px;overflow:auto}.row{padding:10px 0;border-bottom:1px solid #242932;font-size:13px}.pill{display:inline-block;padding:3px 8px;border-radius:20px;background:#242a33;margin-right:8px}.mono{font-family:ui-monospace,monospace}.small{color:#9da5af;font-size:12px}.install{display:none;position:sticky;top:10px;z-index:10;width:100%;box-sizing:border-box;margin-bottom:14px;padding:12px 14px;border:1px solid #3a424d;border-radius:12px;background:#181d24;color:#eee;font-weight:700}.install button{float:right;border:0;border-radius:8px;padding:7px 12px;background:#eee;color:#111;font-weight:700}</style></head><body><main><div id="install" class="install">📱 Install Dragon on this phone <button id="installBtn">Install</button></div><h1>🐉 Dragon Arbitrage</h1><div class="sub">Live engine monitor • refreshes every 2 seconds</div><div id="status" class="card">Loading...</div><br><div id="cards" class="grid"></div><div class="card feed"><b>Live Activity</b><div id="feed"></div></div></main><script>let deferredPrompt=null;const installBox=document.getElementById('install'),installBtn=document.getElementById('installBtn');window.addEventListener('beforeinstallprompt',e=>{e.preventDefault();deferredPrompt=e;installBox.style.display='block'});installBtn.onclick=async()=>{if(!deferredPrompt)return;deferredPrompt.prompt();await deferredPrompt.userChoice;deferredPrompt=null;installBox.style.display='none'};window.addEventListener('appinstalled',()=>installBox.style.display='none');if('serviceWorker'in navigator)navigator.serviceWorker.register('/service-worker.js').catch(()=>{});async function tick(){try{const r=await fetch('/health?x='+Date.now());const j=await r.json(),s=j.state||{};document.getElementById('status').innerHTML='<span class="pill '+(s.ws_connected?'good':'bad')+'">● '+(s.ws_connected?'BINANCE CONNECTED':'BINANCE DISCONNECTED')+'</span><span class="pill '+(j.live&&!j.dry_run?'good':'warn')+'">'+(j.live&&!j.dry_run?'LIVE TRADING':'NOT LIVE')+'</span><span class="small">Updated '+new Date().toLocaleTimeString()+'</span>';const vals=[['Triangles',s.triangles],['Symbols',s.symbols],['Opportunities',s.opportunities],['Executions',s.executions],['Errors',s.execution_errors],['Risk blocks',s.risk_blocks],['Reconnects',s.reconnects]];document.getElementById('cards').innerHTML=vals.map(x=>'<div class="card"><div class="label">'+x[0]+'</div><div class="value">'+(x[1]??0)+'</div></div>').join('');const feed=(s.recent||[]).slice().reverse();document.getElementById('feed').innerHTML=feed.map(e=>'<div class="row"><span class="small">'+new Date(e.ts*1000).toLocaleTimeString()+'</span> <span class="pill">'+e.kind+'</span> '+e.message+' '+(e.path?'<span class="mono">'+e.path+'</span>':'')+(e.net_bps!=null?' <b>'+Number(e.net_bps).toFixed(3)+' bps</b>':'')+'</div>').join('')||'<div class="small">Waiting for activity...</div>'}catch(e){document.getElementById('status').innerHTML='<span class="bad">Dashboard connection error</span>'}}tick();setInterval(tick,2000)</script></body></html>'''


async def stream_loop(cfg, client, filters, triangles, symbols):
    books, triangles_by_symbol = {}, {}
    for triangle in triangles:
        for symbol in triangle.symbols: triangles_by_symbol.setdefault(symbol, []).append(triangle)
    last_order, failures, reconnect_delay = 0.0, 0, 1
    while True:
        try:
            async with websockets.connect(cfg.ws_base,ping_interval=10,ping_timeout=30,close_timeout=5,max_size=2**23) as ws:
                STATE["ws_connected"] = True; event("WS","Binance WebSocket connected")
                print("BINANCE WS CONNECTED",flush=True)
                for i in range(0,len(symbols),200):
                    await ws.send(json.dumps({"method":"SUBSCRIBE","params":[f"{s.lower()}@bookTicker" for s in symbols[i:i+200]],"id":i//200+1}))
                reconnect_delay=1
                while True:
                    msg=json.loads(await ws.recv())
                    if "s" not in msg or "b" not in msg or "a" not in msg: continue
                    symbol=msg["s"]; books[symbol]={"bidPrice":msg["b"],"bidQty":msg["B"],"askPrice":msg["a"],"askQty":msg["A"],"ts":time.time()*1000}; now=time.time()*1000
                    for triangle in triangles_by_symbol.get(symbol,()):
                        if not all(s in books and now-books[s]["ts"]<=cfg.stale_ms for s in triangle.symbols): continue
                        result=evaluate_triangle(triangle,books,cfg.fee_bps,cfg.slippage_bps)
                        if not result: continue
                        net_bps,gross_bps,path,first,second=result
                        if net_bps<Decimal(str(cfg.min_net_edge_bps)): continue
                        STATE["opportunities"]+=1; STATE["last_opportunity"]=time.time(); event("OPPORTUNITY",f"net={net_bps:.3f} gross={gross_bps:.3f}",path=path,net_bps=float(net_bps),gross_bps=float(gross_bps))
                        if not(cfg.live_trading and not cfg.dry_run) or now-last_order<cfg.cooldown_ms: continue
                        try:
                            account=client.account(); free=next((Decimal(x["free"]) for x in account.get("balances",[]) if x["asset"]=="USDT"),Decimal("0")); budget=risk_budget(free,cfg.risk_pct,cfg.max_notional_usdt)
                            if not approved(net_bps,cfg.min_net_edge_bps,budget,cfg.max_notional_usdt) or budget<=0:
                                STATE["risk_blocks"]+=1; event("RISK","Trade blocked by risk/notional gate",path=path); continue
                            event("LIVE","Executing arbitrage",path=path,net_bps=float(net_bps)); execution=await asyncio.to_thread(execute_triangle,client,path,first,second,budget,filters,False); STATE["executions"]+=1; STATE["last_execution"]=time.time(); failures=0; event("FILLED","Execution complete",path=path)
                        except Exception as exc:
                            failures+=1; STATE["execution_errors"]+=1; STATE["last_error"]=str(exc); event("ERROR",str(exc),path=path); print(f"EXECUTION ERROR | failures={failures} | {exc}",flush=True)
                            if failures>=3: return
                        finally: last_order=time.time()*1000
        except Exception as exc:
            STATE["ws_connected"]=False; STATE["reconnects"]+=1; STATE["last_error"]=str(exc); event("WS_ERROR",str(exc)); print(f"WS ERROR | reconnect_in={reconnect_delay}s | {exc}",flush=True); await asyncio.sleep(reconnect_delay); reconnect_delay=min(reconnect_delay*2,30)


async def run():
    cfg=Config.from_env(); cfg.validate(); STATE["started_at"]=time.time(); STATE["live"]=cfg.live_trading; STATE["dry_run"]=cfg.dry_run; start_health_server(); client=BinanceClient(cfg.api_base,os.getenv("BINANCE_API_KEY",""),os.getenv("BINANCE_API_SECRET","")); info=client.exchange_info(); filters=make_filters(info); triangles=build_triangles(info,cfg.max_triangles); symbols=sorted({s for t in triangles for s in t.symbols}); STATE["triangles"]=len(triangles); STATE["symbols"]=len(symbols); print(f"DRAGON STARTED | triangles={len(triangles)} symbols={len(symbols)} dry_run={cfg.dry_run} live={cfg.live_trading}",flush=True); await stream_loop(cfg,client,filters,triangles,symbols)


def make_filters(info):
    out={}
    for s in info.get("symbols",[]):
        fs={f["filterType"]:f for f in s.get("filters",[])}; lot=fs.get("LOT_SIZE",{}); market_lot=fs.get("MARKET_LOT_SIZE",{}); out[s["symbol"]]={"baseAsset":s["baseAsset"],"quoteAsset":s["quoteAsset"],"stepSize":market_lot.get("stepSize",lot.get("stepSize","0.00000001")),"minQty":market_lot.get("minQty",lot.get("minQty","0"))}
    return out


def main(): asyncio.run(run())
if __name__=="__main__": main()
