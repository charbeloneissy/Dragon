import asyncio
import json
import os
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.binance import BinanceClient
from src.dragon.config import Config
from src.dragon.control import snapshot, set_control
from src.dragon.executor import execute_triangle
from src.dragon.ledger import Ledger
from src.dragon.risk import approved, risk_budget
from src.dragon.triangles import build_triangles, evaluate_triangle

STATE = {
    "started_at": None, "status": "starting", "ws_connected": False,
    "triangles": 0, "symbols": 0, "quote_updates": 0, "depth_updates": 0,
    "scans": 0, "opportunities": 0, "executions": 0, "execution_errors": 0,
    "risk_blocks": 0, "reconnects": 0, "last_opportunity": None,
    "last_execution": None, "last_error": None, "recent": [],
    "live": False, "dry_run": True, "binance_authenticated": False,
    "free_usdt": "0", "realized_pnl_usdt": 0.0, "ledger_filled": 0,
    "futures_enabled": False, "futures_live": False, "futures_universe": 0,
    "futures_scans": 0, "futures_opportunities": 0, "futures_executions": 0,
    "futures_errors": 0, "futures_last_error": None, "futures_last_scan": None,
}
LOCK = Lock()
SERVER = None
LEDGER = None


def event(kind, message, **data):
    item = {"ts": time.time(), "kind": kind, "message": message, **data}
    with LOCK:
        STATE["recent"] = (STATE["recent"] + [item])[-100:]
    print(f"DRAGON {kind} | {message}", flush=True)


def _control_payload():
    c = snapshot()
    warnings = []
    if c["kill_switch"]:
        warnings.append("KILL SWITCH ACTIVE: new trading execution is blocked")
    if not c["master"]:
        warnings.append("Master execution is OFF")
    if not c["spot"]:
        warnings.append("Spot execution is OFF")
    if not c["futures"]:
        warnings.append("Futures execution is OFF")
    if not c["analysis"]:
        warnings.append("Data analysis is OFF")
    with LOCK:
        if not STATE["ws_connected"]:
            warnings.append("Spot market WebSocket is disconnected")
        if STATE["last_error"]:
            warnings.append(str(STATE["last_error"]))
        if STATE["futures_last_error"]:
            warnings.append(str(STATE["futures_last_error"]))
    if c["warning"]:
        warnings.append(c["warning"])
    return c, list(dict.fromkeys(warnings))


class Handler(BaseHTTPRequestHandler):
    def _json(self, code, payload):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = DASHBOARD.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path in ("/health", "/healthz") or self.path.startswith("/health?"):
            with LOCK:
                state = dict(STATE)
            controls, warnings = _control_payload()
            state["controls"] = controls
            state["warnings"] = warnings
            self._json(200 if state["status"] in ("running", "starting", "degraded") else 503,
                       {"status": state["status"], "service": "dragon", "state": state})
            return
        if self.path in ("/dashboard", "/dashboard/"):
            body = DASHBOARD.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path == "/control":
            controls, warnings = _control_payload()
            self._json(200, {"controls": controls, "warnings": warnings}); return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        if self.path != "/control":
            self.send_response(404); self.end_headers(); return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            name = data.get("name")
            if name == "warning":
                value = str(data.get("value", ""))[:500]
            else:
                if name not in {"master", "spot", "futures", "analysis", "kill_switch"}:
                    raise ValueError("invalid control")
                value = bool(data.get("value"))
            controls = set_control(name, value)
            event("CONTROL", f"{name}={'ON' if value else 'OFF'}")
            warnings = _control_payload()[1]
            self._json(200, {"controls": controls, "warnings": warnings})
        except Exception as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, *_args):
        return


def start_health_server():
    global SERVER
    if SERVER is not None:
        return SERVER
    SERVER = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Handler)
    Thread(target=SERVER.serve_forever, daemon=True).start()
    return SERVER


DASHBOARD = r'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dragon Live Control</title><style>body{margin:0;background:#080a0d;color:#eee;font:14px system-ui}main{max-width:1200px;margin:auto;padding:16px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}.card{background:#12161c;border:1px solid #252b34;border-radius:14px;padding:14px;margin-bottom:12px}.v{font-size:24px;font-weight:700}.muted{color:#89929d}.good{color:#54d88b}.warn{color:#f0c75e}.bad{color:#ff6674}.btn{border:1px solid #38404b;background:#1b2129;color:#fff;border-radius:10px;padding:11px 13px;margin:4px;cursor:pointer;font-weight:700}.on{border-color:#54d88b}.off{border-color:#ff6674}.kill{background:#5c1118;border-color:#ff6674}.row{padding:8px 0;border-bottom:1px solid #252b34;font-size:12px}.mono{font-family:monospace}.warnbox{background:#2b2108;border:1px solid #80671b;padding:12px;border-radius:10px;margin-bottom:12px}.title{display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap}</style><main><div class=title><h1>🐉 Dragon Live Control</h1><span class=muted id=clock></span></div><div id=warning></div><div class=card id=status>Loading...</div><div class=card><b>Runtime controls</b><div><button class=btn id=master onclick="setc('master',!C.master)"></button><button class=btn id=spot onclick="setc('spot',!C.spot)"></button><button class=btn id=futures onclick="setc('futures',!C.futures)"></button><button class=btn id=analysis onclick="setc('analysis',!C.analysis)"></button><button class="btn kill" onclick="kill()">☠ KILL SWITCH</button></div><div class=muted>Controls block new executions in this single process. They do not force-close an existing position.</div></div><div id=cards class=grid></div><div class=card><b>Live activity</b><div id=feed></div></div></main><script>let C={};async function setc(name,value){await fetch('/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,value})});tick()}async function kill(){if(confirm('Activate emergency execution lock?'))await setc('kill_switch',true)}function button(id,label,key){let e=document.getElementById(id);e.textContent=label+': '+(C[key]?'ON':'OFF');e.className='btn '+(C[key]?'on':'off')}async function tick(){try{let j=await(await fetch('/health?'+Date.now())).json(),s=j.state;C=s.controls||{};document.getElementById('clock').textContent=new Date().toLocaleTimeString();document.getElementById('status').innerHTML='<b class="'+(s.ws_connected?'good':'bad')+'">'+(s.ws_connected?'● SPOT DATA CONNECTED':'● SPOT DATA DISCONNECTED')+'</b> &nbsp; <b class="'+(s.binance_authenticated?'good':'bad')+'">'+(s.binance_authenticated?'● BINANCE AUTHENTICATED':'● BINANCE NOT AUTHENTICATED')+'</b> &nbsp; <b class="'+(s.live&&!s.dry_run?'good':'warn')+'">'+(s.live&&!s.dry_run?'LIVE CONFIGURED':'SAFE CONFIGURED')+'</b> &nbsp; USDT '+s.free_usdt+' &nbsp; P&L '+Number(s.realized_pnl_usdt||0).toFixed(6);button('master','MASTER','master');button('spot','SPOT','spot');button('futures','FUTURES','futures');button('analysis','ANALYSIS','analysis');let a=[['Spot triangles',s.triangles],['Spot symbols',s.symbols],['Spot scans',s.scans],['Spot opportunities',s.opportunities],['Spot executions',s.executions],['Futures universe',s.futures_universe],['Futures scans',s.futures_scans],['Futures opportunities',s.futures_opportunities],['Futures executions',s.futures_executions],['Errors',s.execution_errors+s.futures_errors],['Risk blocks',s.risk_blocks],['Realized P&L',Number(s.realized_pnl_usdt||0).toFixed(6)]];document.getElementById('cards').innerHTML=a.map(x=>'<div class=card><span class=muted>'+x[0]+'</span><div class=v>'+x[1]+'</div></div>').join('');document.getElementById('warning').innerHTML=(s.warnings||[]).map(w=>'<div class=warnbox>⚠️ '+w+'</div>').join('');document.getElementById('feed').innerHTML=(s.recent||[]).slice().reverse().map(e=>'<div class=row><span class=muted>'+new Date(e.ts*1000).toLocaleTimeString()+'</span> <b>'+e.kind+'</b> '+e.message+(e.path?' <span class=mono>'+e.path.join(' → ')+'</span>':'')+(e.net_bps!=null?' <b>'+Number(e.net_bps).toFixed(3)+' bps</b>':'')).join('')||'<span class=muted>Waiting...</span>'}catch(e){document.getElementById('status').innerHTML='<b class=bad>Dashboard connection error</b>'}}tick();setInterval(tick,2000)</script>'''


def _symbol_meta(info):
    return {s["symbol"]: (s["baseAsset"], s["quoteAsset"]) for s in info.get("symbols", []) if s.get("status") == "TRADING"}


def make_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING": continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {}); market_lot = fs.get("MARKET_LOT_SIZE", {})
        notional = fs.get("NOTIONAL", fs.get("MIN_NOTIONAL", {}))
        out[s["symbol"]] = {"baseAsset": s["baseAsset"], "quoteAsset": s["quoteAsset"],"stepSize": market_lot.get("stepSize", lot.get("stepSize", "0.00000001")),"minQty": market_lot.get("minQty", lot.get("minQty", "0")),"maxQty": market_lot.get("maxQty", lot.get("maxQty", "0")),"minNotional": notional.get("minNotional", "0"),"maxNotional": notional.get("maxNotional", "0")}
    return out


async def stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    """Fallback single-process loop. Production replaces this with full_universe_runner."""
    await asyncio.sleep(0)
    raise RuntimeError("production engine did not install full-universe stream loop")


def _sync_ledger():
    if LEDGER is None: return
    summary = LEDGER.summary()
    with LOCK:
        STATE["ledger_filled"] = summary["filled"]
        STATE["realized_pnl_usdt"] = summary["realized_pnl_usdt"]


async def run():
    global LEDGER
    cfg = Config.from_env(); cfg.validate(); start_health_server()
    with LOCK:
        STATE["started_at"] = time.time(); STATE["live"] = cfg.live_trading; STATE["dry_run"] = cfg.dry_run; STATE["status"] = "starting"
    LEDGER = Ledger()
    api_key = os.getenv("BINANCE_API_KEY", "").strip(); api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    client = BinanceClient(cfg.api_base, api_key, api_secret)
    try:
        if cfg.live_trading and not cfg.dry_run:
            if not api_key or not api_secret: raise RuntimeError("LIVE_TRADING requires BINANCE_API_KEY and BINANCE_API_SECRET")
            account = client.account()
            with LOCK: STATE["binance_authenticated"] = True
            free = next((x.get("free", "0") for x in account.get("balances", []) if x.get("asset") == "USDT"), "0")
            with LOCK: STATE["free_usdt"] = str(free)
            event("AUTH", "Binance API authenticated successfully", usdt_free=str(free))
        else: event("SAFE", "Live execution disabled")
        info = client.exchange_info(); filters = make_filters(info); symbol_meta = _symbol_meta(info)
        triangles = build_triangles(info, cfg.max_triangles); symbols = sorted({s for t in triangles for s in t.symbols})
        with LOCK: STATE["triangles"] = len(triangles); STATE["symbols"] = len(symbols)
        event("START", f"Dragon triangular engine ready: triangles={len(triangles)} symbols={len(symbols)} live={cfg.live_trading and not cfg.dry_run}")
        await stream_loop(cfg, client, filters, triangles, symbols, symbol_meta)
    finally: client.close()


def main(): asyncio.run(run())

if __name__ == "__main__": main()
