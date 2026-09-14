from dataclasses import dataclass

@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    def validate(self) -> None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            raise ValueError(f"invalid quote for {self.symbol}")

@dataclass(frozen=True)
class Opportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_price: float
    sell_price: float
    gross_edge_bps: float
    net_edge_bps: float
    notional_usdt: float
    @property
    def profitable(self) -> bool:
        return self.net_edge_bps > 0
