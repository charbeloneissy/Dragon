import asyncio
import time
from decimal import Decimal


def main():
    from src.dragon import main as dragon_main
    from src.dragon.hardening import install
    from src.dragon.dashboard import HTML as DASHBOARD_HTML
    import websockets

    install(dragon_main)
    dragon_main.DASHBOARD = DASHBOARD_HTML

    original_connect = websockets.connect

    def resilient_connect(*args, **kwargs):
        kwargs["ping_interval"] = 10
        kwargs["ping_timeout"] = 30
        kwargs["close_timeout"] = 5
        return original_connect(*args, **kwargs)

    websockets.connect = resilient_connect

    original_get = dragon_main.Handler.do_GET

    def dashboard_root(self):
        if self.path == "/":
            self.path = "/dashboard"
            try:
                return original_get(self)
            finally:
                self.path = "/"
        return original_get(self)

    dragon_main.Handler.do_GET = dashboard_root

    # Evaluator telemetry plus a short-lived signal cache used by the final
    # order gate. This does not alter the executable-edge formula.
    original_evaluate = dragon_main.evaluate_triangle
    last_signal = {}

    def instrumented_evaluate(*args, **kwargs):
        result = original_evaluate(*args, **kwargs)
        try:
            with dragon_main.LOCK:
                dragon_main.STATE.setdefault("rejection", {})
                dragon_main.STATE.setdefault("rejection_total", 0)
                dragon_main.STATE["rejection_total"] += 1
                if result is None:
                    key = "NO_EXECUTABLE_DEPTH"
                else:
                    net_bps, _gross_bps, path, _first, _second = result
                    cfg = dragon_main.Config.from_env()
                    key = "NET_EDGE_REJECTED" if Decimal(str(net_bps)) < Decimal(str(cfg.min_net_edge_bps)) else "NET_EDGE_PASSED"
                    if key == "NET_EDGE_PASSED":
                        last_signal[tuple(path)] = (time.monotonic() * 1000, Decimal(str(net_bps)))
                dragon_main.STATE["rejection"][key] = dragon_main.STATE["rejection"].get(key, 0) + 1
        except Exception:
            pass
        return result

    dragon_main.evaluate_triangle = instrumented_evaluate

    original_approved = dragon_main.approved

    def guarded_approved(net_bps, min_net_bps, notional, max_notional, *, min_trade_notional=None):
        if not original_approved(net_bps, min_net_bps, notional, max_notional, min_trade_notional=min_trade_notional):
            try:
                with dragon_main.LOCK:
                    dragon_main.STATE.setdefault("rejection", {})
                    dragon_main.STATE["rejection"]["RISK_OR_NOTIONAL"] = dragon_main.STATE["rejection"].get("RISK_OR_NOTIONAL", 0) + 1
                dragon_main.event("RISK", "Trade blocked by risk/notional gate")
            except Exception:
                pass
            return False

        cfg = dragon_main.Config.from_env()
        expected_profit = Decimal(str(notional)) * Decimal(str(net_bps)) / Decimal("10000")
        minimum_profit = Decimal(str(cfg.min_expected_profit_usdt))
        if expected_profit < minimum_profit:
            try:
                with dragon_main.LOCK:
                    dragon_main.STATE.setdefault("rejection", {})
                    dragon_main.STATE["rejection"]["EXPECTED_PROFIT_REJECTED"] = dragon_main.STATE["rejection"].get("EXPECTED_PROFIT_REJECTED", 0) + 1
                dragon_main.event("GATE", "Trade blocked: expected profit below minimum", expected_profit=float(expected_profit), minimum_profit=float(minimum_profit), net_bps=float(net_bps))
            except Exception:
                pass
            return False
        return True

    dragon_main.approved = guarded_approved

    original_execute = dragon_main.execute_triangle

    def guarded_execute(client, path, start_asset, first_asset, budget, filters, dry_run):
        cfg = dragon_main.Config.from_env()
        now_ms = time.monotonic() * 1000
        signal = last_signal.get(tuple(path))
        max_age_ms = min(float(cfg.stale_ms), 250.0)
        if signal is None or now_ms - signal[0] > max_age_ms:
            try:
                with dragon_main.LOCK:
                    dragon_main.STATE.setdefault("rejection", {})
                    dragon_main.STATE["rejection"]["FINAL_SIGNAL_STALE"] = dragon_main.STATE["rejection"].get("FINAL_SIGNAL_STALE", 0) + 1
                dragon_main.event("GATE", "Final signal freshness check blocked order", age_ms=float(now_ms - signal[0]) if signal else None, max_age_ms=max_age_ms)
            except Exception:
                pass
            raise RuntimeError("final signal freshness check failed")

        if not (cfg.live_trading and not cfg.dry_run):
            return original_execute(client, path, start_asset, first_asset, budget, filters, dry_run)

        # Final authenticated balance recheck. The periodic scanner balance is
        # not trusted for the actual order decision.
        account = client.account()
        free_usdt = Decimal(str(next((x.get("free", "0") for x in account.get("balances", []) if x.get("asset") == "USDT"), "0")))
        reserve = Decimal(str(cfg.safety_reserve_usdt))
        if free_usdt - reserve < Decimal(str(budget)):
            try:
                with dragon_main.LOCK:
                    dragon_main.STATE.setdefault("rejection", {})
                    dragon_main.STATE["rejection"]["FINAL_BALANCE_REJECTED"] = dragon_main.STATE["rejection"].get("FINAL_BALANCE_REJECTED", 0) + 1
                    dragon_main.STATE["free_usdt"] = str(free_usdt)
                dragon_main.event("GATE", "Final balance recheck blocked order", free_usdt=str(free_usdt), reserve=str(reserve), budget=str(budget))
            except Exception:
                pass
            raise RuntimeError("final balance recheck failed: executable balance below budget plus safety reserve")
        return original_execute(client, path, start_asset, first_asset, budget, filters, dry_run)

    dragon_main.execute_triangle = guarded_execute

    # Dragon is explicitly Spot-only. Ignore stale Futures environment flags.
    async def supervisor():
        await dragon_main.run()

    asyncio.run(supervisor())


if __name__ == "__main__":
    main()
