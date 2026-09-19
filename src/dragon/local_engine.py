from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from .local_amm import V2PoolState, V3PoolState, optimize_unimodal
from .local_pool_state import LocalPoolState, PoolRecord

@dataclass(frozen=True)
class LocalOpportunity:
    pool_buy: str
    pool_sell: str
    base_token: str
    quote_token: str
    amount_in: int
    amount_mid: int
    amount_out: int
    gross_profit: int
    age_ms: float
    confidence: str = "local"

class LocalArbitrageEngine:
    """CPU-only arbitrage search over a WebSocket-fed pool snapshot.

    No eth_call is made while searching. The engine fails closed on unsupported
    V3 tick-crossing situations rather than producing optimistic quotes.
    """

    def __init__(self,state:LocalPoolState,max_state_age_ms:float=1500.0,min_profit:int=1):
        self.state=state
        self.max_state_age_ms=float(max_state_age_ms)
        self.min_profit=int(min_profit)

    @staticmethod
    def _quote(pool:PoolRecord, token_in:str, amount:int)->int:
        if pool.kind=="v2":
            return pool.state.quote(token_in,amount)
        if pool.kind=="v3":
            return pool.state.quote(token_in, amount)
        return 0

    async def find_best(self,quote_token:str,base_token:str,max_input:int)->LocalOpportunity|None:
        snap=await self.state.snapshot()
        buys=[]; sells=[]
        for p in snap.values():
            if p.age_ms>self.max_state_age_ms:
                continue
            pair={p.token0.lower(),p.token1.lower()}
            if pair != {quote_token.lower(),base_token.lower()}:
                continue
            buys.append(p); sells.append(p)
        best=None
        for a in buys:
            for b in sells:
                if a.address.lower()==b.address.lower():
                    continue
                def profit(x:int)->int:
                    mid=self._quote(a,quote_token,x)
                    if mid<=0:return -10**100
                    out=self._quote(b,base_token,mid)
                    return out-x
                amount,profit=optimize_unimodal(profit,1,max_input)
                if profit<self.min_profit: continue
                mid=self._quote(a,quote_token,amount)
                out=self._quote(b,base_token,mid)
                age=max(a.age_ms,b.age_ms)
                candidate=LocalOpportunity(a.address,b.address,base_token,quote_token,amount,mid,out,profit,age)
                if best is None or candidate.gross_profit>best.gross_profit: best=candidate
        return best
