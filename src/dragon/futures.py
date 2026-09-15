from decimal import Decimal
from typing import Dict, List, Tuple


class FuturesOpportunity:
    def __init__(self, symbol: str, spot_bid: Decimal, spot_ask: Decimal, futures_bid: Decimal, futures_ask: Decimal, fee_bps: Decimal):
        self.symbol = symbol
        self.spot_bid = spot_bid
        self.spot_ask = spot_ask
        self.futures_bid = futures_bid
        self.futures_ask = futures_ask
        self.fee_bps = fee_bps

    def cash_and_carry_bps(self) -> Decimal:
        if self.spot_ask <= 0 or self.futures_bid <= 0:
            return Decimal("-999999")
        gross = (self.futures_bid / self.spot_ask - Decimal("1")) * Decimal("10000")
        return gross - self.fee_bps * Decimal("2")


def evaluate_basis(rows: List[dict], fee_bps: Decimal, min_edge_bps: Decimal) -> List[Tuple[str, Decimal]]:
    opportunities = []
    for row in rows:
        try:
            symbol = row["symbol"]
            spot_ask = Decimal(str(row["spotAsk"]))
            spot_bid = Decimal(str(row["spotBid"]))
            futures_bid = Decimal(str(row["futuresBid"]))
            futures_ask = Decimal(str(row["futuresAsk"]))
            opp = FuturesOpportunity(symbol, spot_bid, spot_ask, futures_bid, futures_ask, fee_bps)
            edge = opp.cash_and_carry_bps()
            if edge >= min_edge_bps:
                opportunities.append((symbol, edge))
        except (KeyError, ValueError, ArithmeticError):
            continue
    return sorted(opportunities, key=lambda x: x[1], reverse=True)
