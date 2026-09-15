from decimal import Decimal


def approved(
    net_bps: Decimal,
    min_net_bps: float,
    notional: Decimal,
    max_notional: float,
    *,
    max_position: Decimal | None = None,
    current_position: Decimal = Decimal("0"),
) -> bool:
    """Final live-trade gate.

    Never allow the requested notional to push the resulting position above
    Binance's configured MAX_POSITION limit when that limit is supplied.
    """
    if net_bps < Decimal(str(min_net_bps)):
        return False
    if not Decimal("0") < notional <= Decimal(str(max_notional)):
        return False
    if max_position is not None:
        if max_position <= 0 or current_position + notional > max_position:
            return False
    return True


def risk_budget(free_usdt: Decimal, risk_pct: float, max_notional: float) -> Decimal:
    # Live exposure is intentionally capped at 0.25% by default. The hard
    # upper ceiling remains 1% so a bad environment value cannot create a
    # larger risk budget than the existing safety policy permits.
    pct = max(Decimal("0.0005"), min(Decimal(str(risk_pct)), Decimal("0.01")))
    return min(free_usdt * pct, Decimal(str(max_notional)))
