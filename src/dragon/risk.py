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


def risk_budget(free_usdt: Decimal, risk_pct: float, max_notional: float) -> Decimal:
    # Risk remains a ceiling. Never inflate the budget merely to reach the
    # exchange minimum notional; the caller must skip when the budget is too small.
    pct = max(Decimal("0.0005"), min(Decimal(str(risk_pct)), Decimal("0.01")))
    return min(free_usdt * pct, Decimal(str(max_notional)))
