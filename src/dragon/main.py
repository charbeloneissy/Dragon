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
    "market_streams": 0, "subscription_acks": 0, "balance_refreshes": 0,
    "min_notional_blocks": 0,
}
LOCK = Lock()
SERVER = None
LEDGER = None


def event(kind, message, **data):
    item = {"ts": time.time(), "kind": kind, "message": message, **data}
    with LOCK:
        STATE["recent"] = (STATE["recent"] + [item])[-100:]
    print(f"DRAGON {kind} | {message}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard")
            self.end_headers(); return
        if self.path in ("/health", "/healthz") or self.path.startswith("/health?"):
            with LOCK:
                payload = {"status": STATE["status"], "service": "dragon", "state": dict(STATE)}
            body = json.dumps(payload, default=str).encode()
            self.send_response(200 if payload["status"] in ("running", "starting", "degraded") else 503)
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


DASHBOARD = r'''<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><title>Dragon Arbitrage</title><style>body{margin:0;background:#0b0d10;color:#eee;font:14px system-ui}main{max-width:1100px;margin:auto;padding:20px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}.card{background:#14181e;border:1px solid #252b34;border-radius:12px;padding:14px;margin-bottom:12px}.v{font-size:25px;font-weight:700}.muted{color:#8f98a3}.good{color:#55d68a}.warn{color:#f0c75e}.bad{color:#ff6874}.row{padding:8px 0;border-bottom:1px solid #252b34;font-size:12px}.mono{font-family:monospace}</style><main><h1>🐉 Dragon Arbitrage</h1><div id=s class=card>Loading...</div><div id=c class=grid></div><div class=card><b>Live activity</b><div id=f></div></div></main><script>async function tick(){try{let j=await(await fetch('/health?'+Date.now())).json(),s=j.state;document.getElementById('s').innerHTML='<b class="'+(s.ws_connected?'good':'bad')+'">'+(s.ws_connected?'● BINANCE WS CONNECTED':'● BINANCE WS DISCONNECTED')+'</b> &nbsp; <b class="'+(s.binance_authenticated?'good':'bad')+'">'+(s.binance_authenticated?'● BINANCE API AUTHENTICATED':'● BINANCE API NOT AUTHENTICATED')+'</b> &nbsp; <b class="'+(s.live&&!s.dry_run?'good':'warn')+'">'+(s.live&&!s.dry_run?'LIVE EXECUTION':'PAPER/SAFE')+'</b> &nbsp; <span class=muted>USDT '+s.free_usdt+' | P&L '+Number(s.realized_pnl_usdt||0).toFixed(6)+'</span>';let a=[['Triangles',s.triangles],['Symbols',s.symbols],['Streams',s.market_streams],['Depth updates',s.depth_updates],['Scans',s.scans],['Opportunities',s.opportunities],['Executions',s.executions],['Errors',s.execution_errors],['Risk blocks',s.risk_blocks],['Min-notional blocks',s.min_notional_blocks],['Reconnects',s.reconnects],['Filled ledger',s.ledger_filled]];document.getElementById('c').innerHTML=a.map(x=>'<div class=card><span class=muted>'+x[0]+'</span><div class=v>'+x[1]+'</div></div>').join('');document.getElementById('f').innerHTML=(s.recent||[]).slice().reverse().map(e=>'<div class=row><span class=muted>'+new Date(e.ts*1000).toLocaleTimeString()+'</span> <b>'+e.kind+'</b> '+e.message+(e.path?' <span class=mono>'+e.path.join(' → ')+'</span>':'')+(e.net_bps!=null?' <b>'+Number(e.net_bps).toFixed(3)+' bps</b>':'')).join('')||'<span class=muted>Waiting...</span>'}catch(e){document.getElementById('s').innerHTML='<b class=bad>Dashboard error</b>'}}tick();setInterval(tick,2000)</script>'''


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


def _sync_ledger():
    if LEDGER is None:
        return
    summary = LEDGER.summary()
    with LOCK:
        STATE["ledger_filled"] = summary["filled"]
        STATE["realized_pnl_usdt"] = summary["realized_pnl_usdt"]


def _ticker_volumes(client):
    try:
        rows = client.ticker_24hr()
        return {r.get("symbol"): Decimal(str(r.get("quoteVolume", "0"))) for r in rows if r.get("symbol")}
    except Exception as exc:
        event("UNIVERSE", f"24h ticker ranking unavailable; using exchange order: {exc}")
        return {}


def _select_stream_universe(triangles, volumes, cap):
    """Select complete triangles without misinterpreting 0 as a 3-symbol cap.

    cap <= 0 means no application-level stream cap. Positive caps rank complete
    triangles by combined 24h quote volume and never split a triangle.
    """
    if not triangles:
        return [], []
    if int(cap) <= 0:
        symbols = sorted({s for triangle in triangles for s in triangle.symbols})
        return list(triangles), symbols

    cap = max(3, int(cap))
    ranked = sorted(
        triangles,
        key=lambda t: sum((volumes.get(s, Decimal("0")) for s in t.symbols), Decimal("0")),
        reverse=True,
    )
    selected_symbols = set()
    selected_triangles = []
    for triangle in ranked:
        additions = set(triangle.symbols) - selected_symbols
        if len(selected_symbols) + len(additions) > cap:
            continue
        selected_symbols.update(triangle.symbols)
        selected_triangles.append(triangle)
        if len(selected_symbols) >= cap:
            break
    if not selected_triangles:
        selected_triangles = [ranked[0]]
        selected_symbols = set(ranked[0].symbols)
    return selected_triangles, sorted(selected_symbols)


async def _refresh_balance(client):
    account = await asyncio.to_thread(client.account)
    free = next((Decimal(str(x.get("free", "0"))) for x in account.get("balances", []) if x.get("asset") == "USDT"), Decimal("0"))
    with LOCK:
        STATE["free_usdt"] = str(free)
        STATE["balance_refreshes"] += 1
    return free


async def stream_loop(cfg, client, filters, triangles, symbols, symbol_meta):
    books, dirty = {}, set()
    by_symbol = {}
    for i, t in enumerate(triangles):
        for s in t.symbols:
            by_symbol.setdefault(s, []).append(i)
    reconnect_delay = 1
    last_order_ms = 0.0
    last_balance_ms = time.monotonic() * 1000
    free_usdt = Decimal("0")
    balance_ok = True
    failures = 0

    while True:
        try:
            async with websockets.connect(
                cfg.ws_base,
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5,
                max_size=2**23,
            ) as ws:
                with LOCK:
                    STATE["ws_connected"] = True
                    STATE["status"] = "running"
                    STATE["market_streams"] = len(symbols)
                event("WS", f"Binance market WebSocket connected; streams={len(symbols)}")
                params = [f"{s.lower()}@depth{cfg.depth_levels}@100ms" for s in symbols]
                for i in range(0, len(params), 180):
                    request_id = i // 180 + 1
                    await ws.send(json.dumps({"method": "SUBSCRIBE", "params": params[i:i + 180], "id": request_id}))
                event("WS", f"Market subscriptions requested; count={len(params)} batches={(len(params) + 179) // 180}")
                reconnect_delay = 1

                while True:
                    msg = json.loads(await ws.recv())
                    if "id" in msg and msg.get("result") is None:
                        with LOCK:
                            STATE["subscription_acks"] += 1
                        continue
                    d = _depth_payload(msg, cfg.depth_levels)
                    if not d:
                        continue
                    s, dv = d
                    books.setdefault(s, {}).update(dv)
                    dirty.update(by_symbol.get(s, ()))
                    with LOCK:
                        STATE["depth_updates"] += 1
                    if not dirty:
                        continue
                    now = time.monotonic() * 1000
                    candidates = list(dirty)
                    dirty.clear()
                    with LOCK:
                        STATE["scans"] += len(candidates)

                    if now - last_balance_ms >= 10000:
                        try:
                            free_usdt = await _refresh_balance(client)
                            last_balance_ms = now
                            balance_ok = True
                        except Exception as exc:
                            balance_ok = False
                            event("BALANCE_ERROR", f"balance refresh failed; trading paused until restored: {exc}")
                            last_balance_ms = now
                    if not balance_ok:
                        continue
                    budget = risk_budget(
                        free_usdt,
                        cfg.risk_pct,
                        cfg.max_notional_usdt,
                        Decimal(str(cfg.min_trade_notional_usdt)),
                    )
                    if budget <= 0:
                        with LOCK:
                            STATE["min_notional_blocks"] += 1
                        continue
                    for idx in candidates:
                        t = triangles[idx]
                        if not all(s in books and now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in t.symbols):
                            continue
                        result = evaluate_triangle(t, books, cfg.fee_bps, cfg.max_slippage_bps, symbol_meta, budget)
                        if not result:
                            continue
                        net_bps, gross_bps, path, first, second = result
                        if net_bps < Decimal(str(cfg.min_net_edge_bps)):
                            continue
                        with LOCK:
                            STATE["opportunities"] += 1
                            STATE["last_opportunity"] = time.time()
                        event("OPPORTUNITY", f"net={net_bps:.3f} gross={gross_bps:.3f}", path=path, net_bps=float(net_bps), gross_bps=float(gross_bps))
                        now_ms = time.monotonic() * 1000
                        if not (cfg.live_trading and not cfg.dry_run) or now_ms - last_order_ms < cfg.cooldown_ms:
                            continue
                        if not approved(
                            net_bps,
                            cfg.min_net_edge_bps,
                            budget,
                            cfg.max_notional_usdt,
                            min_trade_notional=Decimal(str(cfg.min_trade_notional_usdt)),
                        ):
                            with LOCK:
                                STATE["risk_blocks"] += 1
                            event("RISK", "Trade blocked by risk/notional gate", path=path)
                            continue
                        last_order_ms = now_ms
                        try:
                            event("LIVE", "Three-leg execution requested", path=path, net_bps=float(net_bps), budget=str(budget))
                            execution = await asyncio.to_thread(execute_triangle, client, path, "USDT", first, budget, filters, False)
                            if not execution.get("finished") or execution.get("final_asset") != "USDT":
                                raise RuntimeError("execution returned without a completed USDT cycle")
                            LEDGER.record(path, budget, execution)
                            with LOCK:
                                STATE["executions"] += 1
                                STATE["last_execution"] = time.time()
                            _sync_ledger()
                            failures = 0
                            event("FILLED", f"Triangle fully filled; realized={execution['realized_pnl_usdt']} USDT", path=path)
                            try:
                                free_usdt = await _refresh_balance(client)
                                last_balance_ms = time.monotonic() * 1000
                                balance_ok = True
                            except Exception as exc:
                                balance_ok = False
                                event("BALANCE_ERROR", f"post-trade balance reconciliation failed: {exc}")
                        except Exception as exc:
                            failures += 1
                            with LOCK:
                                STATE["execution_errors"] += 1
                                STATE["last_error"] = str(exc)
                            if LEDGER is not None:
                                LEDGER.record(path, budget, error=exc)
                            event("ERROR", str(exc), path=path)
                            if failures >= 3:
                                raise RuntimeError("three consecutive execution failures; engine stopped for safety") from exc
        except Exception as exc:
            with LOCK:
                STATE["ws_connected"] = False
                STATE["status"] = "degraded"
                STATE["reconnects"] += 1
                STATE["last_error"] = str(exc)
            event("WS_ERROR", str(exc))
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 15)


async def run():
    cfg = Config.from_env()
    cfg.validate()
    start_health_server()
    with LOCK:
        STATE["started_at"] = time.time()
        STATE["live"] = cfg.live_trading
        STATE["dry_run"] = cfg.dry_run
        STATE["status"] = "starting"
    LEDGER = globals().get("LEDGER")
    if LEDGER is None:
        globals()["LEDGER"] = Ledger()
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
            with LOCK:
                STATE["binance_authenticated"] = True
                STATE["free_usdt"] = next((x.get("free", "0") for x in account.get("balances", []) if x.get("asset") == "USDT"), "0")
            event("AUTH", "Binance API authenticated successfully", usdt_free=STATE["free_usdt"])
        else:
            event("SAFE", "Live execution disabled")
        info = client.exchange_info()
        filters = make_filters(info)
        symbol_meta = _symbol_meta(info)
        triangles = build_triangles(info, cfg.max_triangles)
        event("TRIANGLES", f"Candidate triangle graph built: {len(triangles)}")
        volumes = _ticker_volumes(client)
        triangles, symbols = _select_stream_universe(triangles, volumes, int(os.getenv("STREAM_SYMBOL_CAP", "0")))
        with LOCK:
            STATE["triangles"] = len(triangles)
            STATE["symbols"] = len(symbols)
            STATE["market_streams"] = len(symbols)
        event("UNIVERSE", f"Liquid stream universe selected: triangles={len(triangles)} symbols={len(symbols)}")
        event("START", f"Dragon engine ready; live={cfg.live_trading and not cfg.dry_run}")
        await stream_loop(cfg, client, filters, triangles, symbols, symbol_meta)
    finally:
        client.close()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
