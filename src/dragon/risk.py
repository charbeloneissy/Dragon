from decimal import Decimal


def approved(net_bps: Decimal, min_net_bps: float, notional: Decimal, max_notional: float) -> bool:
    return net_bps >= Decimal(str(min_net_bps)) and Decimal("0") < notional <= Decimal(str(max_notional))


def risk_budget(free_usdt: Decimal, risk_pct: float, max_notional: float) -> Decimal:
    return min(free_usdt * Decimal(str(max(0.0005, min(risk_pct, 0.01)))), Decimal(str(max_notional)))
