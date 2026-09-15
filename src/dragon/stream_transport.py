"""Harden Binance Spot market WebSocket transport and verify data flow."""
from __future__ import annotations

import json
import time
from decimal import Decimal


def _combined_ws_url(url: str) -> str:
    """Normalize Binance market-stream URLs to the combined-stream endpoint."""
    base = url.rstrip("/")
    if base.endswith("/ws"):
        return base[:-3] + "/stream"
    if base.endswith("/stream"):
        return base
    return base + "/stream"


def _depth_payload(msg, levels):
    """Accept raw/combined partial-depth and diff-depth payloads."""
    if not isinstance(msg, dict):
        return None
    data = msg.get("data", msg)
    if not isinstance(data, dict):
        return None

    stream = msg.get("stream", "")
    symbol = data.get("s")
    if not symbol and stream:
        symbol = stream.split("@", 1)[0].upper()
    if not symbol:
        return None

    event_type = data.get("e")
    if event_type not in (None, "depthUpdate"):
        return None

    raw_bids = data.get("b", data.get("bids", [])) or []
    raw_asks = data.get("a", data.get("asks", [])) or []
    bids, asks = [], []
    for pair in raw_bids[:levels]:
        if len(pair) >= 2:
            p, q = Decimal(str(pair[0])), Decimal(str(pair[1]))
            if p > 0 and q > 0:
                bids.append((pair[0], pair[1]))
    for pair in raw_asks[:levels]:
        if len(pair) >= 2:
            p, q = Decimal(str(pair[0])), Decimal(str(pair[1]))
            if p > 0 and q > 0:
                asks.append((pair[0], pair[1]))
    if not bids or not asks:
        return None
    return symbol.upper(), {
        "bids": bids,
        "asks": asks,
        "depth_ts": time.monotonic() * 1000,
    }


def _ensure_state(main):
    defaults = {
        "ws_messages": 0,
        "ws_malformed_messages": 0,
        "subscription_errors": 0,
        "subscription_verified": False,
        "subscribed_streams": 0,
        "first_depth_at": None,
        "last_ws_event_at": None,
        "last_ws_symbol": None,
    }
    with main.LOCK:
        for key, value in defaults.items():
            main.STATE.setdefault(key, value)


class _SocketProxy:
    """Observe the websocket control/data plane without changing the trading loop."""

    VERIFY_ID = 900001

    def __init__(self, websocket, main, expected):
        self._ws = websocket
        self._main = main
        self._expected = set(expected)
        self._verify_sent = False

    def __getattr__(self, name):
        return getattr(self._ws, name)

    async def send(self, payload):
        try:
            obj = json.loads(payload)
        except Exception:
            obj = None
        if isinstance(obj, dict) and obj.get("method") == "SUBSCRIBE":
            params = obj.get("params") or []
            with self._main.LOCK:
                self._main.STATE["subscribed_streams"] += len(params)
            self._main.event("WS", f"Subscription batch sent; id={obj.get('id')} count={len(params)}")
        return await self._ws.send(payload)

    async def recv(self):
        raw = await self._ws.recv()
        with self._main.LOCK:
            self._main.STATE["ws_messages"] += 1
            self._main.STATE["last_ws_event_at"] = time.time()

        try:
            msg = json.loads(raw)
        except Exception:
            with self._main.LOCK:
                self._main.STATE["ws_malformed_messages"] += 1
            return raw

        if isinstance(msg, dict) and "id" in msg:
            if msg.get("id") == self.VERIFY_ID:
                result = msg.get("result")
                active = set(result) if isinstance(result, list) else set()
                verified = self._expected.issubset(active)
                with self._main.LOCK:
                    self._main.STATE["subscription_verified"] = verified
                    self._main.STATE["subscription_acks"] += 1 if msg.get("error") is None else 0
                if verified:
                    self._main.event("WS", f"Subscription verification passed; active={len(active)} expected={len(self._expected)}")
                else:
                    self._main.event("WS_ERROR", f"Subscription verification failed; active={len(active)} expected={len(self._expected)}")
                return raw

            if msg.get("error") is not None:
                with self._main.LOCK:
                    self._main.STATE["subscription_errors"] += 1
                    self._main.STATE["last_error"] = str(msg.get("error"))
                self._main.event("WS_ERROR", f"Binance subscription error: {msg.get('error')}")
            elif msg.get("result") is None:
                with self._main.LOCK:
                    self._main.STATE["subscription_acks"] += 1

        depth = _depth_payload(msg, 1)
        if depth:
            symbol, _ = depth
            first = False
            with self._main.LOCK:
                if self._main.STATE.get("first_depth_at") is None:
                    self._main.STATE["first_depth_at"] = time.time()
                    first = True
                self._main.STATE["last_ws_symbol"] = symbol
            if first:
                self._main.event("WS", f"First Binance depth event received; symbol={symbol}")
        elif isinstance(msg, dict) and "stream" not in msg and "result" not in msg and "error" not in msg:
            with self._main.LOCK:
                self._main.STATE["ws_malformed_messages"] += 1
        return raw

    async def verify(self):
        if self._verify_sent:
            return
        self._verify_sent = True
        await self._ws.send(json.dumps({"method": "LIST_SUBSCRIPTIONS", "id": self.VERIFY_ID}))
        self._main.event("WS", "Subscription verification requested")

    async def __aenter__(self):
        entered = await self._ws.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return await self._ws.__aexit__(exc_type, exc, tb)



def install(main_module):
    """Patch the production Spot stream loop with transport and subscription telemetry."""
    if getattr(main_module, "_spot_stream_transport_fixed", False):
        return

    _ensure_state(main_module)
    original_payload = main_module._depth_payload
    original_loop = main_module.stream_loop
    original_connect = main_module.websockets.connect

    def connect(*args, **kwargs):
        url = kwargs.get("uri") if "uri" in kwargs else (args[0] if args else "")
        expected = []
        # Main sends subscriptions after connect, so derive the expected list later from send().
        socket = original_connect(*args, **kwargs)

        class ConnectProxy:
            def __init__(self, inner):
                self.inner = inner
                self.proxy = None

            async def __aenter__(self):
                ws = await self.inner.__aenter__()
                self.proxy = _SocketProxy(ws, main_module, expected)
                return self.proxy

            async def __aexit__(self, exc_type, exc, tb):
                return await self.inner.__aexit__(exc_type, exc, tb)

        return ConnectProxy(socket)

    main_module.websockets.connect = connect

    async def patched_loop(cfg, client, filters, triangles, symbols, symbol_meta):
        main_module._depth_payload = _depth_payload
        patched_cfg = cfg.__class__(
            api_base=cfg.api_base,
            ws_base=_combined_ws_url(cfg.ws_base),
            dry_run=cfg.dry_run,
            live_trading=cfg.live_trading,
            min_net_edge_bps=cfg.min_net_edge_bps,
            max_notional_usdt=cfg.max_notional_usdt,
            max_slippage_bps=cfg.max_slippage_bps,
            fee_bps=cfg.fee_bps,
            risk_pct=cfg.risk_pct,
            cooldown_ms=cfg.cooldown_ms,
            max_triangles=cfg.max_triangles,
            stale_ms=cfg.stale_ms,
            poll_interval_seconds=cfg.poll_interval_seconds,
            order_timeout_ms=cfg.order_timeout_ms,
            depth_levels=cfg.depth_levels,
            health_fail_open=cfg.health_fail_open,
        )
        try:
            return await original_loop(patched_cfg, client, filters, triangles, symbols, symbol_meta)
        finally:
            main_module._depth_payload = original_payload
            main_module.websockets.connect = original_connect

    main_module.stream_loop = patched_loop
    main_module._spot_stream_transport_fixed = True
