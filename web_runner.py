import asyncio
import json
import os
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

import websockets

from src.dragon.binance import BinanceClient
from src.dragon.config import Config
from src.dragon.executor import execute_triangle
from src.dragon.risk import approved, risk_budget
from src.dragon.triangles import build_triangles, evaluate_triangle

STATE = {"started_at": None, "ws_connected": False, "triangles": 0, "symbols": 0, "quote_updates": 0, "depth_updates": 0, "scans": 0, "opportunities": 0, "executions": 0, "execution_errors": 0, "risk_blocks": 0, "reconnects": 0, "last_opportunity": None, "last_execution": None, "last_error": None, "recent": [], "live": False, "dry_run": True, "binance_authenticated": False}
LOCK = Lock()


def event(kind, message, **data):
    item = {"ts": time.time(), "kind": kind, "message": message, **data}
    with LOCK:
        STATE["recent"] = (STATE["recent"] + [item])[-60:]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz"):
            with LOCK:
                payload = {"status": "ok", "service": "dragon", "state": dict(STATE)}
            body = json.dumps(payload, default=str).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if self.path in ("/dashboard", "/dashboard/"):
            body = DASHBOARD.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self, *_args): return


def start_health_server():
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


DASHBOARD = r'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dragon Arbitrage</title><style>body{margin:0;background:#0b0d10;color:#eee;font:14px system-ui}main{max-width:1100px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{background:#14181e;border:1px solid #252b34;border-radius:12px;padding:14px;margin-bottom:12px}.v{font-size:25px;font-weight:700}.muted{color:#8f98a3}.good{color:#55d68a}.warn{color:#f0c75e}.bad{color:#ff6874}.row{padding:8px 0;border-bottom:1px solid #252b34;font-size:12px}.mono{font-family:monospace}</style><main><h1>🐉 Dragon Arbitrage</h1><div id=s class=card>Loading...</div><div id=c class=grid></div><div class=card><b>Live activity</b><div id=f></div></div></main><script>async function tick(){try{let j=await(await fetch('/health?'+Date.now())).json(),s=j.state;document.getElementById('s').innerHTML='<b class="'+(s.ws_connected?'good':'bad')+'">'+(s.ws_connected?'● BINANCE WS CONNECTED':'● BINANCE WS DISCONNECTED')+'</b> &nbsp; <b class="'+(s.binance_authenticated?'good':'bad')+'">'+(s.binance_authenticated?'● BINANCE API AUTHENTICATED':'● BINANCE API NOT AUTHENTICATED')+'</b> &nbsp; <b class="'+(s.live&&!s.dry_run?'good':'warn')+'">'+(s.live&&!s.dry_run?'LIVE EXECUTION':'PAPER/SAFE')+'</b> &nbsp; <span class=muted>quotes '+s.quote_updates+' / depth '+s.depth_updates+'</span>';let a=[['Triangles',s.triangles],['Symbols',s.symbols],['Scans',s.scans],['Opportunities',s.opportunities],['Executions',s.executions],['Errors',s.execution_errors],['Risk blocks',s.risk_blocks],['Reconnects',s.reconnects]];document.getElementById('c').innerHTML=a.map(x=>'<div class=card><span class=muted>'+x[0]+'</span><div class=v>'+x[1]+'</div></div>').join('');document.getElementById('f').innerHTML=(s.recent||[]).slice().reverse().map(e=>'<div class=row><span class=muted>'+new Date(e.ts*1000).toLocaleTimeString()+'</span> <b>'+e.kind+'</b> '+e.message+(e.path?' <span class=mono>'+e.path.join(' → ')+'</span>':'')+(e.net_bps!=null?' <b>'+Number(e.net_bps).toFixed(3)+' bps</b>':'')).join('')||'<span class=muted>Waiting...</span>'}catch(e){document.getElementById('s').innerHTML='<b class=bad>Dashboard error</b>'}}tick();setInterval(tick,2000)</script>'''


def _symbol_meta(info):
    return {s["symbol"]: (s["baseAsset"], s["quoteAsset"]) for s in info.get("symbols", [])}


def _depth_payload(msg):
    d = msg.get("data", msg)
    if "s" not in d: return None
    bids = [(float(p), float(q)) for p, q in d.get("b", [])[:5] if float(p) > 0 and float(q) > 0]
    asks = [(float(p), float(q)) for p, q in d.get("a", [])[:5] if float(p) > 0 and float(q) > 0]
    if not bids or not asks: return None
    return d["s"], {"bids": bids, "asks": asks, "depth_ts": time.monotonic() * 1000}


def _quote_payload(msg):
    d = msg.get("data", msg)
    if not all(k in d for k in ("s", "b", "a")): return None
    return d["s"], {"bidPrice": d["b"], "bidQty": d.get("B", "0"), "askPrice": d["a"], "askQty": d.get("A", "0"), "quote_ts": time.monotonic() * 1000}


async def stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    books, dirty = {}, set()
    by_symbol = {}
    for i, t in enumerate(triangles):
        for s in t.symbols: by_symbol.setdefault(s, []).append(i)
    reconnect_delay = 1; last_order_ms = 0.0; failures = 0
    while True:
        try:
            async with websockets.connect(cfg.ws_base, ping_interval=20, ping_timeout=10, close_timeout=5, max_size=2**23) as ws:
                STATE["ws_connected"] = True; event("WS", "Binance Spot WebSocket connected")
                params = [x for s in symbols for x in (f"{s.lower()}@bookTicker", f"{s.lower()}@depth5@100ms")]
                for i in range(0, len(params), 180): await ws.send(json.dumps({"method":"SUBSCRIBE","params":params[i:i+180],"id":i//180+1}))
                reconnect_delay = 1
                while True:
                    msg = json.loads(await ws.recv()); q = _quote_payload(msg); d = _depth_payload(msg)
                    if q:
                        s, qv = q; books.setdefault(s, {}).update(qv); dirty.update(by_symbol.get(s, ())); STATE["quote_updates"] += 1
                    if d:
                        s, dv = d; books.setdefault(s, {}).update(dv); dirty.update(by_symbol.get(s, ())); STATE["depth_updates"] += 1
                    if not dirty: continue
                    now = time.monotonic() * 1000; candidates = list(dirty); dirty.clear(); STATE["scans"] += 1
                    for idx in candidates:
                        t = triangles[idx]
                        if not all(s in books and now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in t.symbols): continue
                        result = evaluate_triangle(t, books, cfg.fee_bps, cfg.max_slippage_bps, symbol_meta, max(cfg.max_notional_usdt * cfg.risk_pct, Decimal("0.01")))
                        if not result: continue
                        net_bps, gross_bps, path, first, second = result
                        if net_bps < Decimal(str(cfg.min_net_edge_bps)): continue
                        STATE["opportunities"] += 1; STATE["last_opportunity"] = time.time(); event("OPPORTUNITY", f"net={net_bps:.3f} gross={gross_bps:.3f}", path=path, net_bps=float(net_bps), gross_bps=float(gross_bps))
                        now_ms = time.monotonic() * 1000
                        if not (cfg.live_trading and not cfg.dry_run) or now_ms - last_order_ms < cfg.cooldown_ms: continue
                        try:
                            account = client.account(); free = next((Decimal(x["free"]) for x in account.get("balances", []) if x["asset"] == "USDT"), Decimal("0")); budget = risk_budget(free, cfg.risk_pct, cfg.max_notional_usdt)
                            if budget <= 0 or not approved(net_bps, cfg.min_net_edge_bps, budget, cfg.max_notional_usdt):
                                STATE["risk_blocks"] += 1; event("RISK", "Trade blocked by risk/notional gate", path=path); continue
                            event("LIVE", "Execution requested", path=path, net_bps=float(net_bps)); await asyncio.to_thread(execute_triangle, client, path, first, second, budget, filters, False)
                            STATE["executions"] += 1; STATE["last_execution"] = time.time(); failures = 0; event("FILLED", "Triangle execution returned successfully", path=path)
                        except Exception as exc:
                            failures += 1; STATE["execution_errors"] += 1; STATE["last_error"] = str(exc); event("ERROR", str(exc), path=path)
                            if failures >= 3: raise RuntimeError("three consecutive execution failures; engine stopped for safety") from exc
                        finally: last_order_ms = time.monotonic() * 1000
        except Exception as exc:
            STATE["ws_connected"] = False; STATE["reconnects"] += 1; STATE["last_error"] = str(exc); event("WS_ERROR", str(exc)); await asyncio.sleep(reconnect_delay); reconnect_delay = min(reconnect_delay * 2, 15)


async def run():
    cfg = Config.from_env(); cfg.validate(); STATE["started_at"] = time.time(); STATE["live"] = cfg.live_trading; STATE["dry_run"] = cfg.dry_run; start_health_server()
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    client = BinanceClient(cfg.api_base, api_key, api_secret)
    if cfg.live_trading and not cfg.dry_run:
        if not api_key or not api_secret:
            raise RuntimeError("LIVE_TRADING is enabled but BINANCE_API_KEY/BINANCE_API_SECRET are not configured in the runtime environment")
        account = client.account()
        STATE["binance_authenticated"] = True
        free_usdt = next((x.get("free", "0") for x in account.get("balances", []) if x.get("asset") == "USDT"), "0")
        event("AUTH", "Binance API authenticated successfully", usdt_free=str(free_usdt))
        print("BINANCE API AUTHENTICATED | live trading enabled", flush=True)
    info = client.exchange_info(); filters = make_filters(info); symbol_meta = _symbol_meta(info)
    triangles = build_triangles(info, cfg.max_triangles); symbols = sorted({s for t in triangles for s in t.symbols}); STATE["triangles"] = len(triangles); STATE["symbols"] = len(symbols)
    event("START", f"Dragon ultra triangular engine started: triangles={len(triangles)} symbols={len(symbols)}"); print(f"DRAGON STARTED | triangles={len(triangles)} symbols={len(symbols)} dry_run={cfg.dry_run} live={cfg.live_trading}", flush=True)
    await stream_loop(cfg, client, filters, triangles, symbols, symbol_meta)


def make_filters(info):
    out = {}
    for s in info.get("symbols", []):
        fs = {f["filterType"]: f for f in s.get("filters", [])}; lot = fs.get("LOT_SIZE", {}); market_lot = fs.get("MARKET_LOT_SIZE", {})
        out[s["symbol"]] = {"baseAsset": s["baseAsset"], "quoteAsset": s["quoteAsset"], "stepSize": market_lot.get("stepSize", lot.get("stepSize", "0.00000001")), "minQty": market_lot.get("minQty", lot.get("minQty", "0"))}
    return out


def main(): asyncio.run(run())


if __name__ == "__main__": main()
