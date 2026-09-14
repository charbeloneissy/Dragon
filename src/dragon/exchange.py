from abc import ABC, abstractmethod
from .models import Quote

class Exchange(ABC):
    name: str
    @abstractmethod
    def quote(self, symbol: str) -> Quote:
        raise NotImplementedError
    def execute_arbitrage(self, symbol: str, quantity: float, buy_price: float, sell_price: float) -> None:
        raise RuntimeError("live execution is not implemented in Phase 1")
