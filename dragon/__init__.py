"""Dragon package bootstrap.

Initialize dashboard telemetry fields before the production runner starts.
"""

import web_runner
from src.dragon.control import STATE as CONTROL_STATE

with web_runner.LOCK:
    web_runner.STATE.setdefault("balance_refreshes", 0)
    web_runner.STATE.setdefault("min_notional_blocks", 0)
    web_runner.STATE.setdefault("ws_shards", 0)
    web_runner.STATE.setdefault("ws_shard_size", 0)
    web_runner.STATE.setdefault("ws_disconnects", 0)
    web_runner.STATE.setdefault("ws_reconnects", 0)
    web_runner.STATE.setdefault("ws_last_disconnect", None)
    web_runner.STATE.setdefault("ws_next_retry_at", None)
    web_runner.STATE.setdefault("ws_shard_status", {})

# Dragon is Spot-only in production. Keep the dashboard control consistent with
# the Render configuration, where Futures execution is explicitly disabled.
with web_runner.LOCK:
    CONTROL_STATE["futures"] = False
