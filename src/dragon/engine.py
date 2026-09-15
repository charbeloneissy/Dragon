from .arbitrage import find_opportunity
from .config import Config
from .models import Quote
from .risk import approve

def evaluate(symbol: str, buy_exchange: str, buy_quote: Quote, sell_exchange: str, sell_quote: Quote, config: Config):
    opportunity = find_opportunity(symbol, buy_exchange, buy_quote, sell_exchange, sell_quote, config.fee_bps, config.max_slippage_bps, config.max_notional_usdt)
    if opportunity and approve(opportunity, config.min_net_edge_bps, config.max_notional_usdt):
        return opportunity
    return None
