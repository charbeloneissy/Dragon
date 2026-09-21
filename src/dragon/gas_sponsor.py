"""Gas sponsorship decision layer for Cloud B.

A sponsor quote is an execution resource, not trading profit. The manager
selects the lowest-cost valid sponsorship while preserving Dragon's minimum
trading-profit floor. It never creates a claim of free gas.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class SponsorQuote:
    sponsor_id: str
    chain_id: int
    max_gas_native: int
    sponsor_fee_quote: Decimal
    expires_at: float
    available: bool = True


@dataclass(frozen=True)
class SponsorDecision:
    approved: bool
    sponsor_id: str | None
    sponsor_cost_quote: Decimal
    reason: str


class GasSponsorManager:
    def __init__(self, *, min_net_profit: Decimal = Decimal("0.005")):
        self.min_net_profit = Decimal(min_net_profit)

    def choose(
        self,
        quotes: Iterable[SponsorQuote],
        *,
        expected_net_before_sponsor: Decimal,
        now: float | None = None,
    ) -> SponsorDecision:
        now = time.time() if now is None else now
        valid = [
            q for q in quotes
            if q.available and q.expires_at > now and q.max_gas_native > 0
            and q.sponsor_fee_quote >= 0 and q.sponsor_fee_quote.is_finite()
        ]
        if not valid:
            return SponsorDecision(False, None, Decimal("0"), "no_valid_sponsor")
        valid.sort(key=lambda q: q.sponsor_fee_quote)
        chosen = valid[0]
        realized_after = Decimal(expected_net_before_sponsor) - chosen.sponsor_fee_quote
        if realized_after < self.min_net_profit:
            return SponsorDecision(False, chosen.sponsor_id, chosen.sponsor_fee_quote,
                                   "sponsorship_would_breach_profit_floor")
        return SponsorDecision(True, chosen.sponsor_id, chosen.sponsor_fee_quote, "sponsor_selected")
