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
    *,
    capital_allocation_pct: float | None = None,
    safety_reserve_usdt: Decimal = Decimal("0"),
) -> Decimal:
    """Calculate executable arbitrage capital independently from directional risk.

    Triangular arbitrage sizing is capital allocation, not a stop-loss risk
    percentage. ``capital_allocation_pct`` therefore takes precedence when
    supplied. The legacy ``risk_pct`` remains the fallback for compatibility.
    """
    if free_usdt <= 0:
        return Decimal("0")
    reserve = max(Decimal("0"), safety_reserve_usdt)
    available = max(Decimal("0"), free_usdt - reserve)
    if available <= 0:
        return Decimal("0")
    if capital_allocation_pct is None:
        pct = max(Decimal("0"), min(Decimal(str(risk_pct)), Decimal("0.01")))
    else:
        pct = max(Decimal("0"), min(Decimal(str(capital_allocation_pct)), Decimal("1")))
    if pct <= 0:
        return Decimal("0")
    budget = min(available * pct, Decimal(str(max_notional)))
    if min_trade_notional is not None and budget < min_trade_notional:
        return Decimal("0")
    return budget
