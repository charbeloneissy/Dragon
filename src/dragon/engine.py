from .arbitrage import find_opportunity
from .config import Config
from .models import Quote
from .risk import approved


def evaluate(symbol: str, buy_exchange: str, buy_quote: Quote, sell_exchange: str, sell_quote: Quote, config: Config):
    opportunity = find_opportunity(symbol, buy_exchange, buy_quote, sell_exchange, sell_quote, config.fee_bps, config.slippage_bps, config.max_notional_usdt)
    if opportunity and approved(opportunity.net_edge_bps, config.min_net_edge_bps, opportunity.notional_usdt, config.max_notional_usdt):
        return opportunity
    return None


def main():
    # Render's configured command is `python -m dragon.engine`.
    # Delegate to the realtime Binance runner while keeping this module import-safe.
    import sys
    import os
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if root not in sys.path:
        sys.path.insert(0, root)
    from web_runner import main as runner_main
    runner_main()


if __name__ == "__main__":
    main()
