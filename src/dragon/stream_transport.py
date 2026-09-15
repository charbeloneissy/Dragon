"""Runtime fix for Binance Spot partial-depth stream envelopes.

Binance's partial-depth streams use `bids`/`asks` and, on the raw `/ws`
endpoint, do not include the symbol in the payload. The combined `/stream`
endpoint wraps the payload with the stream name, which gives us a reliable
symbol while preserving complete top-of-book snapshots for the existing
triangle evaluator.
"""
from dataclasses import replace
from decimal import Decimal


def _combined_ws_url(url: str) -> str:
    base = url.rstrip("/")
    if base.endswith("/ws"):
        return base[:-3] + "/stream"
    if base.endswith("/stream"):
        return base
    return base + "/stream"


def _depth_payload(msg, levels):
    data = msg.get("data", msg)
    stream = msg.get("stream", "")

    symbol = data.get("s")
    if not symbol and stream:
        symbol = stream.split("@", 1)[0].upper()
    if not symbol:
        return None

    raw_bids = data.get("bids", data.get("b", []))
    raw_asks = data.get("asks", data.get("a", []))
    bids = []
    asks = []
    for pair in raw_bids[:levels]:
        if len(pair) >= 2 and Decimal(str(pair[0])) > 0 and Decimal(str(pair[1])) > 0:
            bids.append((pair[0], pair[1]))
    for pair in raw_asks[:levels]:
        if len(pair) >= 2 and Decimal(str(pair[0])) > 0 and Decimal(str(pair[1])) > 0:
            asks.append((pair[0], pair[1]))
    if not bids or not asks:
        return None
    import time
    return symbol, {"bids": bids, "asks": asks, "depth_ts": time.monotonic() * 1000}


def install(main_module):
    """Patch the existing stream loop without changing execution/risk logic."""
    original_payload = main_module._depth_payload
    original_loop = main_module.stream_loop

    if getattr(main_module, "_spot_stream_transport_fixed", False):
        return

    async def patched_loop(cfg, client, filters, triangles, symbols, symbol_meta):
        main_module._depth_payload = _depth_payload
        patched_cfg = replace(cfg, ws_base=_combined_ws_url(cfg.ws_base))
        try:
            return await original_loop(patched_cfg, client, filters, triangles, symbols, symbol_meta)
        finally:
            main_module._depth_payload = original_payload

    main_module.stream_loop = patched_loop
    main_module._spot_stream_transport_fixed = True
