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
}
LOCK = Lock()
SERVER = None
LEDGER = None


def event(kind, message, **data):
    item = {"ts": time.time(), "kind": kind, "message": message, **data}
    with LOCK:
        STATE["recent"] = (STATE["recent"] + [item])[-100:]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/healthz") or self.path.startswith("/health?"):
            with LOCK:
                payload = {"status": STATE["status"], "service": "dragon", "state": dict(STATE)}
            body = json.dumps(payload, default=str).encode()
            self.send_response(200 if payload["status"] in ("running", "starting") else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if self.path in ("/dashboard", "/dashboard/"):
            body = DASHBOARD.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()

    def log_message(self, *_args):
        return


def start_health_server():
    global SERVER
    if SERVER is not None:
        return SERVER
    SERVER = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), Handler)
    Thread(target=SERVER.serve_forever, daemon=True).start()
    return SERVER


DASHBOARD = r'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dragon Arbitrage</title><style>body{margin:0;background:#0b0d10;color:#eee;font:14px system-ui}main{max-width:1100px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{background:#14181e;border:1px solid #252b34;border-radius:12px;padding:14px;margin-bottom:12px}.v{font-size:25px;font-weight:700}.muted{color:#8f98a3}.good{color:#55d68a}.warn{color:#f0c75e}.bad{color:#ff6874}.row{padding:8px 0;border-bottom:1px solid #252b34;font-size:12px}.mono{font-family:monospace}</style><main><h1>🐉 Dragon Arbitrage</h1><div id=s class=card>Loading...</div><div id=c class=grid></div><div class=card><b>Live activity</b><div id=f></div></div></main><script>async function tick(){try{let j=await(await fetch('/health?'+Date.now())).json(),s=j.state;document.getElementById('s').innerHTML='<b class="'+(s.ws_connected?'good':'bad')+'">'+(s.ws_connected?'● BINANCE WS CONNECTED':'● BINANCE WS DISCONNECTED')+'</b> &nbsp; <b class="'+(s.binance_authenticated?'good':'bad')+'">'+(s.binance_authenticated?'● BINANCE API AUTHENTICATED':'● BINANCE API NOT AUTHENTICATED')+'</b> &nbsp; <b class="'+(s.live&&!s.dry_run?'good':'warn')+'">'+(s.live&&!s.dry_run?'LIVE EXECUTION':'PAPER/SAFE')+'</b> &nbsp; <span class=muted>USDT '+s.free_usdt+' | P&L '+Number(s.realized_pnl_usdt||0).toFixed(6)+'</span>';let a=[['Triangles',s.triangles],['Symbols',s.symbols],['Scans',s.scans],['Opportunities',s.opportunities],['Executions',s.executions],['Errors',s.execution_errors],['Risk blocks',s.risk_blocks],['Reconnects',s.reconnects],['Filled ledger',s.ledger_filled]];document.getElementById('c').innerHTML=a.map(x=>'<div class=card><span class=muted>'+x[0]+'</span><div class=v>'+x[1]+'</div></div>').join('');document.getElementById('f').innerHTML=(s.recent||[]).slice().reverse().map(e=>'<div class=row><span class=muted>'+new Date(e.ts*1000).toLocaleTimeString()+'</span> <b>'+e.kind+'</b> '+e.message+(e.path?' <span class=mono>'+e.path.join(' → ')+'</span>':'')+(e.net_bps!=null?' <b>'+Number(e.net_bps).toFixed(3)+' bps</b>':'')).join('')||'<span class=muted>Waiting...</span>'}catch(e){document.getElementById('s').innerHTML='<b class=bad>Dashboard error</b>'}}tick();setInterval(tick,2000)</script>'''


def _symbol_meta(info):
    return {s["symbol"]: (s["baseAsset"], s["quoteAsset"]) for s in info.get("symbols", []) if s.get("status") == "TRADING"}


def make_filters(info):
    out = {}
    for s in info.get("symbols", []):
        if s.get("status") != "TRADING":
            continue
        fs = {f["filterType"]: f for f in s.get("filters", [])}
        lot = fs.get("LOT_SIZE", {})
        market_lot = fs.get("MARKET_LOT_SIZE", {})
        notional = fs.get("NOTIONAL", fs.get("MIN_NOTIONAL", {}))
        price = fs.get("PRICE_FILTER", {})
        out[s["symbol"]] = {
            "baseAsset": s["baseAsset"], "quoteAsset": s["quoteAsset"],
            "stepSize": market_lot.get("stepSize", lot.get("stepSize", "0.00000001")),
            "minQty": market_lot.get("minQty", lot.get("minQty", "0")),
            "maxQty": market_lot.get("maxQty", lot.get("maxQty", "0")),
            "minNotional": notional.get("minNotional", "0"),
            "maxNotional": notional.get("maxNotional", "0"),
            "applyMinToMarket": notional.get("applyMinToMarket", True),
            "applyMaxToMarket": notional.get("applyMaxToMarket", True),
            "minPrice": price.get("minPrice", "0"),
            "maxPrice": price.get("maxPrice", "0"),
            "tickSize": price.get("tickSize", "0"),
        }
    return out


def _depth_payload(msg, levels):
    d = msg.get("data", msg)
    if "s" not in d:
        return None
    bids = [(p, q) for p, q in d.get("b", [])[:levels] if Decimal(str(p)) > 0 and Decimal(str(q)) > 0]
    asks = [(p, q) for p, q in d.get("a", [])[:levels] if Decimal(str(p)) > 0 and Decimal(str(q)) > 0]
    if not bids or not asks:
        return None
    return d["s"], {"bids": bids, "asks": asks, "depth_ts": time.monotonic() * 1000}


def _quote_payload(msg):
    d = msg.get("data", msg)
    if not all(k in d for k in ("s", "b", "a")):
        return None
    return d["s"], {"bidPrice": d["b"], "bidQty": d.get("B", "0"), "askPrice": d["a"], "askQty": d.get("A", "0"), "quote_ts": time.monotonic() * 1000}


def _sync_ledger():
    if LEDGER is None:
        return
    summary = LEDGER.summary()
    with LOCK:
        STATE["ledger_filled"] = summary["filled"]
        STATE["realized_pnl_usdt"] = summary["realized_pnl_usdt"]


async def stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    books, dirty = {}, set()
    by_symbol = {}
    for i, t in enumerate(triangles):
        for s in t.symbols:
            by_symbol.setdefault(s, []).append(i)
    reconnect_delay = 1
    last_order_ms = 0.0
    last_balance_ms = 0.0
    free_usdt = Decimal("0")
    failures = 0

    while True:
        try:
            async with websockets.connect(cfg.ws_base, ping_interval=20, ping_timeout=10, close_timeout=5, max_size=2**23) as ws:
                with LOCK: STATE["ws_connected"] = True; STATE["status"] = "running"
                event("WS", "Binance Spot WebSocket connected")
                params = [x for s in symbols for x in (f"{s.lower()}@bookTicker", f"{s.lower()}@depth{cfg.depth_levels}@100ms")]
                for i in range(0, len(params), 180):
                    await ws.send(json.dumps({"method": "SUBSCRIBE", "params": params[i:i+180], "id": i // 180 + 1}))
                reconnect_delay = 1
                while True:
                    msg = json.loads(await ws.recv())
                    q = _quote_payload(msg); d = _depth_payload(msg, cfg.depth_levels)
                    if q:
                        s, qv = q; books.setdefault(s, {}).update(qv); dirty.update(by_symbol.get(s, ())); STATE["quote_updates"] += 1
                    if d:
                        s, dv = d; books.setdefault(s, {}).update(dv); dirty.update(by_symbol.get(s, ())); STATE["depth_updates"] += 1
                    if not dirty:
                        continue
                    now = time.monotonic() * 1000
                    candidates = list(dirty); dirty.clear(); STATE["scans"] += len(candidates)
                    for idx in candidates:
                        t = triangles[idx]
                        if not all(s in books and now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in t.symbols):
                            continue
                        if now - last_balance_ms > 500:
                            try:
                                account = await asyncio.to_thread(client.account)
                                free_usdt = next((Decimal(x["free"]) for x in account.get("balances", []) if x.get("asset") == "USDT"), Decimal("0"))
                                last_balance_ms = now
                                with LOCK: STATE["free_usdt"] = str(free_usdt)
                            except Exception as exc:
                                event("BALANCE_ERROR", str(exc)); continue
                        budget = risk_budget(free_usdt, cfg.risk_pct, cfg.max_notional_usdt)
                        if budget <= 0:
                            continue
                        result = evaluate_triangle(t, books, cfg.fee_bps, cfg.max_slippage_bps, symbol_meta, budget)
                        if not result:
                            continue
                        net_bps, gross_bps, path, first, second = result
                        if net_bps < Decimal(str(cfg.min_net_edge_bps)):
                            continue
                        STATE["opportunities"] += 1; STATE["last_opportunity"] = time.time()
                        event("OPPORTUNITY", f"net={net_bps:.3f} gross={gross_bps:.3f}", path=path, net_bps=float(net_bps), gross_bps=float(gross_bps))
                        now_ms = time.monotonic() * 1000
                        if not (cfg.live_trading and not cfg.dry_run) or now_ms - last_order_ms < cfg.cooldown_ms:
                            continue
                        if not approved(net_bps, cfg.min_net_edge_bps, budget, cfg.max_notional_usdt):
                            STATE["risk_blocks"] += 1; event("RISK", "Trade blocked by risk/notional gate", path=path); continue
                        last_order_ms = now_ms
                        try:
                            event("LIVE", "Three-leg execution requested", path=path, net_bps=float(net_bps), budget=str(budget))
                            execution = await asyncio.to_thread(execute_triangle, client, path, "USDT", first, budget, filters, False)
                            if not execution.get("finished") or execution.get("final_asset") != "USDT":
                                raise RuntimeError("execution returned without a completed USDT cycle")
                            LEDGER.record(path, budget, execution)
                            STATE["executions"] += 1; STATE["last_execution"] = time.time(); failures = 0; _sync_ledger()
                            event("FILLED", f"Triangle fully filled; realized={execution['realized_pnl_usdt']} USDT", path=path)
                        except Exception as exc:
                            failures += 1; STATE["execution_errors"] += 1; STATE["last_error"] = str(exc)
                            if LEDGER is not None:
                                LEDGER.record(path, budget, error=exc)
                            event("ERROR", str(exc), path=path)
                            if failures >= 3:
                                raise RuntimeError("three consecutive execution failures; engine stopped for safety") from exc
        except Exception as exc:
            with LOCK: STATE["ws_connected"] = False; STATE["status"] = "degraded"
            STATE["reconnects"] += 1; STATE["last_error"] = str(exc); event("WS_ERROR", str(exc)); await asyncio.sleep(reconnect_delay); reconnect_delay = min(reconnect_delay * 2, 15)


async def run():
    global LEDGER
    cfg = Config.from_env(); cfg.validate(); start_health_server()
    with LOCK:
        STATE["started_at"] = time.time(); STATE["live"] = cfg.live_trading; STATE["dry_run"] = cfg.dry_run; STATE["status"] = "starting"
    LEDGER = Ledger()
    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    client = BinanceClient(cfg.api_base, api_key, api_secret)
    try:
        client.sync_time()
        event("TIME", f"Binance server time synchronized; offset_ms={client.time_offset_ms}")
        if cfg.live_trading and not cfg.dry_run:
            if not api_key or not api_secret:
                raise RuntimeError("LIVE_TRADING requires BINANCE_API_KEY and BINANCE_API_SECRET")
            account = client.account()
            with LOCK: STATE["binance_authenticated"] = True
            free = next((x.get("free", "0") for x in account.get("balances", []) if x.get("asset") == "USDT"), "0")
            with LOCK: STATE["free_usdt"] = str(free)
            event("AUTH", "Binance API authenticated successfully", usdt_free=str(free))
        else:
            event("SAFE", "Live execution disabled")

        info = client.exchange_info()
        filters = make_filters(info); symbol_meta = _symbol_meta(info)
        triangles = build_triangles(info, cfg.max_triangles)
        symbols = sorted({s for t in triangles for s in t.symbols})
        if len(symbols) * 2 > 1000:
            symbols = symbols[:500]
            allowed = set(symbols)
            triangles = [t for t in triangles if all(s in allowed for s in t.symbols)]
        with LOCK:
            STATE["triangles"] = len(triangles); STATE["symbols"] = len(symbols)
        event("START", f"Dragon triangular engine ready: triangles={len(triangles)} symbols={len(symbols)} live={cfg.live_trading and not cfg.dry_run}")
        await stream_loop(cfg, client, filters, triangles, symbols, symbol_meta)
    finally:
        client.close()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
