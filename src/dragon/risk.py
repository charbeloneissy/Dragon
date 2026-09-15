from decimal import Decimal


def approved(
    net_bps: Decimal,
    min_net_bps: float,
    notional: Decimal,
    max_notional: float,
    *,
    min_trade_notional: Decimal | None = None,
) -> bool:
    if net_bps < Decimal(str(min_net_bps)):
        return False
    if not Decimal("0") < notional <= Decimal(str(max_notional)):
        return False
    if min_trade_notional is not None and notional < min_trade_notional:
        return False
    return True


def risk_budget(
    free_usdt: Decimal,
    risk_pct: float,
    max_notional: float,
    min_trade_notional: Decimal | None = None,
) -> Decimal:
    """Return the risk-capped trade budget, or zero when it cannot meet the floor.

    A triangular arbitrage trade must not bypass the configured risk ceiling merely
    to satisfy an exchange minimum notional. Returning zero makes that condition
    explicit to the caller and prevents accidental oversized orders on small accounts.
    """
    if free_usdt <= 0:
        return Decimal("0")
    pct = max(Decimal("0.0005"), min(Decimal(str(risk_pct)), Decimal("0.01")))
    budget = min(free_usdt * pct, Decimal(str(max_notional)))
    if min_trade_notional is not None and budget < min_trade_notional:
        return Decimal("0")
    return budget
