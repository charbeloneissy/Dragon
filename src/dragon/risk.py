from .models import Opportunity

def approve(opportunity: Opportunity, min_net_edge_bps: float, max_notional_usdt: float) -> bool:
    if opportunity.notional_usdt <= 0 or opportunity.notional_usdt > max_notional_usdt:
        return False
    return opportunity.net_edge_bps >= min_net_edge_bps
