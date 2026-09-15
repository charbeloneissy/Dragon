"""Runtime controls shared by the single Dragon process.

Dragon production execution is Spot-only. Futures is deliberately unavailable
through the dashboard control surface so the UI cannot imply that futures
execution is enabled.
"""
from threading import RLock

LOCK = RLock()
STATE = {
    "master": True,
    "spot": True,
    "futures": False,
    "analysis": True,
    "warning": "",
    "kill_switch": False,
}


def snapshot():
    with LOCK:
        return dict(STATE)


def set_control(name, value):
    with LOCK:
        if name not in STATE:
            raise KeyError(name)
        if name == "futures":
            value = False
        STATE[name] = value
        if name == "kill_switch":
            STATE["master"] = not bool(value)
        elif name == "master" and value:
            STATE["kill_switch"] = False
        return dict(STATE)


def trading_allowed(market):
    with LOCK:
        return bool(STATE["master"] and not STATE["kill_switch"] and STATE.get(market, False))


def analysis_allowed():
    with LOCK:
        return bool(STATE["analysis"])
