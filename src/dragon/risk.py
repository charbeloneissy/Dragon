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
    """Calculate a bounded executable budget without spending the reserve.

    ``risk_pct`` is retained for API compatibility and remains a positive
    configuration guard. Directional risk is not multiplied into notional here;
    capital allocation controls the account slice, while max_notional caps the
    trade. The safety reserve is removed before allocation is calculated.
    """
    if free_usdt <= 0:
        return Decimal("0")

    risk = Decimal(str(risk_pct))
    if risk <= 0:
        return Decimal("0")

    reserve = max(Decimal("0"), Decimal(str(safety_reserve_usdt)))
    available = free_usdt - reserve
    if available <= 0:
        return Decimal("0")

    pct = Decimal(str(capital_allocation_pct if capital_allocation_pct is not None else risk_pct))
    if pct <= 0:
        return Decimal("0")

    budget = min(available * pct, Decimal(str(max_notional)))
    if min_trade_notional is not None and budget < min_trade_notional:
        return Decimal("0")
    return budget
