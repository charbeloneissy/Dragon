from .models import Opportunity, Quote

def edge_bps(buy_price: float, sell_price: float) -> float:
    if buy_price <= 0 or sell_price <= 0:
        raise ValueError("prices must be positive")
    return (sell_price - buy_price) / buy_price * 10_000

def find_opportunity(symbol: str, buy_exchange: str, buy_quote: Quote, sell_exchange: str, sell_quote: Quote, fee_bps: float, slippage_bps: float, notional_usdt: float) -> Opportunity | None:
    buy_quote.validate(); sell_quote.validate()
    gross = edge_bps(buy_quote.ask, sell_quote.bid)
    net = gross - (2 * fee_bps) - (2 * slippage_bps)
    if net <= 0:
        return None
    return Opportunity(symbol, buy_exchange, sell_exchange, buy_quote.ask, sell_quote.bid, gross, net, notional_usdt)
