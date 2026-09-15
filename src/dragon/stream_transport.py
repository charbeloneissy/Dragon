"""Harden Binance Spot market WebSocket transport and payload handling."""
import asyncio
import json
import time
from dataclasses import replace
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
    """Accept raw and combined partial-depth/diff-depth payloads."""
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

    raw_bids = data.get("b", data.get("bids", []))
    raw_asks = data.get("a", data.get("asks", []))
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


def _record(main_module, key, value=1):
    with main_module.LOCK:
        main_module.STATE[key] = main_module.STATE.get(key, 0) + value


def install(main_module):
    """Patch the production Spot stream loop without touching execution/risk logic."""
    if getattr(main_module, "_spot_stream_transport_fixed", False):
        return

    original_payload = main_module._depth_payload
    original_loop = main_module.stream_loop

    async def patched_loop(cfg, client, filters, triangles, symbols, symbol_meta):
        main_module._depth_payload = _depth_payload
        patched_cfg = replace(cfg, ws_base=_combined_ws_url(cfg.ws_base))

        # The existing loop already owns reconnect/backoff and execution safety.
        # This wrapper adds protocol-level diagnostics and a scheduled reconnect so
        # a long-lived process never crosses Binance's 24-hour connection lifetime.
        async def run_with_watchdog():
            connected_since = time.monotonic()
            task = asyncio.create_task(
                original_loop(patched_cfg, client, filters, triangles, symbols, symbol_meta)
            )
            try:
                while not task.done():
                    await asyncio.sleep(30)
                    if time.monotonic() - connected_since >= 23 * 60 * 60:
                        task.cancel()
                        raise RuntimeError("scheduled Binance WebSocket rotation")
            finally:
                if not task.done():
                    task.cancel()
            return await task

        try:
            return await run_with_watchdog()
        finally:
            main_module._depth_payload = original_payload

    main_module.stream_loop = patched_loop
    main_module._spot_stream_transport_fixed = True
