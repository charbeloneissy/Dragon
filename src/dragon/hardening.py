"""Production hardening layer for Dragon."""
from __future__ import annotations

import asyncio
import os
import time
from collections import Counter, deque
from decimal import Decimal
from threading import Lock


class CircuitBreaker:
    def __init__(self, failures=3, cooldown_seconds=30.0):
        self.failures = max(1, int(failures))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._count = 0
        self._tripped_until = 0.0
        self._lock = Lock()

    def allow(self):
        with self._lock:
            return time.monotonic() >= self._tripped_until

    def success(self):
        with self._lock:
            self._count = 0
            self._tripped_until = 0.0

    def failure(self):
        with self._lock:
            self._count += 1
            if self._count >= self.failures:
                self._tripped_until = time.monotonic() + self.cooldown_seconds
            return self._count, self._tripped_until

    @property
    def remaining(self):
        with self._lock:
            return max(0.0, self._tripped_until - time.monotonic())


class LatencyMeter:
    def __init__(self, size=200):
        self.values = deque(maxlen=size)
        self._lock = Lock()

    def add(self, ms):
        with self._lock:
            self.values.append(float(ms))

    def summary(self):
        with self._lock:
            vals = sorted(self.values)
        if not vals:
            return {"count": 0, "avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0}
        idx = min(len(vals) - 1, int(len(vals) * 0.95))
        return {"count": len(vals), "avg_ms": round(sum(vals) / len(vals), 3),
                "p95_ms": round(vals[idx], 3), "max_ms": round(vals[-1], 3)}


class DynamicFee:
    def __init__(self, fallback_bps):
        self.bps = Decimal(str(fallback_bps))
        self.fallback = Decimal(str(fallback_bps))
        self.updated_at = 0.0
        self._lock = Lock()

    def refresh(self, client):
        try:
            account = client.account()
            rates = account.get("commissionRates") or {}
            taker = rates.get("taker")
            if taker is not None:
                value = Decimal(str(taker)) * Decimal("10000")
                if value >= 0:
                    with self._lock:
                        self.bps = value
                        self.updated_at = time.time()
                    return value
        except Exception:
            pass
        return self.bps


def _ensure_state(main):
    defaults = {
        "rejection": Counter(), "rejection_total": 0,
        "latency": {"count": 0, "avg_ms": 0.0, "p95_ms": 0.0, "max_ms": 0.0},
        "circuit_breaker_trips": 0, "dynamic_fee_bps": 0.0,
        "last_fee_refresh": None, "last_execution_latency_ms": None,
    }
    with main.LOCK:
        for key, value in defaults.items():
            if key not in main.STATE:
                main.STATE[key] = value.copy() if isinstance(value, (dict, Counter)) else value


def install(main):
    """Patch stable seams without replacing the existing trading architecture."""
    _ensure_state(main)
    original_eval = main.evaluate_triangle
    original_execute = main.execute_triangle
    original_stream = main.stream_loop
    meter = LatencyMeter()
    breaker = CircuitBreaker(
        failures=int(os.getenv("EXECUTION_FAILURE_THRESHOLD", "3")),
        cooldown_seconds=float(os.getenv("EXECUTION_BREAKER_COOLDOWN_SECONDS", "30")),
    )
    fee = DynamicFee(main.Config.from_env().fee_bps)
    context = {"cfg": None}

    def record(reason):
        with main.LOCK:
            counter = main.STATE.setdefault("rejection", Counter())
            counter[reason] += 1
            main.STATE["rejection_total"] = sum(counter.values())

    def diagnostic_eval(t, books, fee_bps, slippage_bps, symbol_meta=None, notional_usdt=1.0):
        cfg = context["cfg"]
        if not all(s in books for s in t.symbols):
            record("NO_LIQUIDITY")
            return None
        now = time.monotonic() * 1000
        if cfg is not None and not all(now - books[s].get("depth_ts", 0) <= cfg.stale_ms for s in t.symbols):
            record("STALE")
            return None
        if not all(books.get(s, {}).get("bids") and books.get(s, {}).get("asks") for s in t.symbols):
            record("NO_LIQUIDITY")
            return None
        effective_fee = fee.bps if fee.updated_at else Decimal(str(fee_bps))
        no_fee = original_eval(t, books, Decimal("0"), Decimal("0"), symbol_meta, notional_usdt)
        if no_fee is None:
            record("NO_LIQUIDITY")
            return None
        with_fee = original_eval(t, books, effective_fee, Decimal("0"), symbol_meta, notional_usdt)
        if with_fee is None:
            record("NO_LIQUIDITY")
            return None
        if with_fee[0] <= 0:
            record("FEE_REJECTED")
        result = original_eval(t, books, effective_fee, slippage_bps, symbol_meta, notional_usdt)
        if result is None:
            record("NO_LIQUIDITY")
            return None
        if result[0] <= 0 and no_fee[0] > 0:
            record("SLIPPAGE_REJECTED")
        elif cfg is not None and result[0] < Decimal(str(cfg.min_net_edge_bps)):
            record("NET_EDGE_REJECTED")
        else:
            record("NET_EDGE_PASSED")
        return result

    def hardened_execute(*args, **kwargs):
        if not breaker.allow():
            with main.LOCK:
                main.STATE["circuit_breaker_trips"] += 1
            raise RuntimeError(f"execution circuit breaker active for {breaker.remaining:.2f}s")
        started = time.perf_counter()
        try:
            result = original_execute(*args, **kwargs)
            elapsed = (time.perf_counter() - started) * 1000
            meter.add(elapsed)
            with main.LOCK:
                main.STATE["latency"] = meter.summary()
                main.STATE["last_execution_latency_ms"] = round(elapsed, 3)
            breaker.success()
            return result
        except Exception:
            elapsed = (time.perf_counter() - started) * 1000
            meter.add(elapsed)
            with main.LOCK:
                main.STATE["latency"] = meter.summary()
            count, until = breaker.failure()
            if until:
                main.event("CIRCUIT", f"execution breaker tripped after {count} failures; cooldown={breaker.cooldown_seconds:.1f}s")
            raise

    async def hardened_stream(cfg, client, filters, triangles, symbols, symbol_meta):
        context["cfg"] = cfg
        fee.fallback = Decimal(str(cfg.fee_bps))
        value = await asyncio.to_thread(fee.refresh, client)
        with main.LOCK:
            main.STATE["dynamic_fee_bps"] = float(value)
            main.STATE["last_fee_refresh"] = time.time()
        main.event("FEE", f"Dynamic Binance taker fee loaded: {value:.4f} bps")

        async def fee_loop():
            while True:
                await asyncio.sleep(float(os.getenv("FEE_REFRESH_SECONDS", "60")))
                value = await asyncio.to_thread(fee.refresh, client)
                with main.LOCK:
                    main.STATE["dynamic_fee_bps"] = float(value)
                    main.STATE["last_fee_refresh"] = time.time()
                    telemetry = {
                        "depth_updates": main.STATE.get("depth_updates", 0),
                        "scans": main.STATE.get("scans", 0),
                        "opportunities": main.STATE.get("opportunities", 0),
                        "executions": main.STATE.get("executions", 0),
                        "balance_refreshes": main.STATE.get("balance_refreshes", 0),
                        "free_usdt": main.STATE.get("free_usdt", "0"),
                        "rejections": dict(main.STATE.get("rejection", {})),
                    }
                main.event("TELEMETRY", f"market={telemetry['depth_updates']} scans={telemetry['scans']} opp={telemetry['opportunities']} exec={telemetry['executions']} balance_refreshes={telemetry['balance_refreshes']} free_usdt={telemetry['free_usdt']} rejections={telemetry['rejections']}")
                main.event("FEE", f"Dynamic Binance taker fee refreshed: {value:.4f} bps")

        task = asyncio.create_task(fee_loop())
        try:
            await original_stream(cfg, client, filters, triangles, symbols, symbol_meta)
        finally:
            task.cancel()
            with main.LOCK:
                main.STATE["latency"] = meter.summary()

    main.evaluate_triangle = diagnostic_eval
    main.execute_triangle = hardened_execute
    main.stream_loop = hardened_stream
    return main
