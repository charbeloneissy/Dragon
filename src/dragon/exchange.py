from abc import ABC, abstractmethod
from .models import Quote


class Exchange(ABC):
    name: str

    @abstractmethod
    def quote(self, symbol: str) -> Quote:
        raise NotImplementedError

    @abstractmethod
    def execute_arbitrage(self, symbol: str, quantity: float, buy_price: float, sell_price: float):
        raise NotImplementedError
