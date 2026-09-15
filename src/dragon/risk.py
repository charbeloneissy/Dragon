from decimal import Decimal


def approved(net_bps: Decimal, min_net_bps: float, notional: Decimal, max_notional: float) -> bool:
    return (
        net_bps >= Decimal(str(min_net_bps))
        and Decimal("0") < notional <= Decimal(str(max_notional))
    )


def risk_budget(free_usdt: Decimal, risk_pct: float, max_notional: float) -> Decimal:
    pct = max(Decimal("0.0005"), min(Decimal(str(risk_pct)), Decimal("0.01")))
    return min(free_usdt * pct, Decimal(str(max_notional)))
