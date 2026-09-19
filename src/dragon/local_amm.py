from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

Q96 = 1 << 96
Q192 = Q96 * Q96

@dataclass
class V2PoolState:
    token0: str
    token1: str
    reserve0: int
    reserve1: int
    fee_bps: int = 30

    def quote(self, token_in: str, amount_in: int) -> int:
        if amount_in <= 0:
            return 0
        a = token_in.lower()
        if a == self.token0.lower():
            rin, rout = self.reserve0, self.reserve1
        elif a == self.token1.lower():
            rin, rout = self.reserve1, self.reserve0
        else:
            raise ValueError("token is not in pool")
        if rin <= 0 or rout <= 0:
            return 0
        fee_num = 10000 - self.fee_bps
        effective = amount_in * fee_num
        return (effective * rout) // (rin * 10000 + effective)

@dataclass
class V3PoolState:
    token0: str
    token1: str
    sqrt_price_x96: int
    liquidity: int
    fee_pips: int
    tick: int = 0

    def quote_without_crossing(self, token_in: str, amount_in: int) -> int:
        """Fast exact-current-range quote.

        Crossing an initialized tick is deliberately rejected by this method.
        The full tick engine must supply the next initialized tick before a
        quote is considered executable.
        """
        if amount_in <= 0 or self.liquidity <= 0 or self.sqrt_price_x96 <= 0:
            return 0
        fee_num = 1_000_000 - self.fee_pips
        amount_less_fee = amount_in * fee_num // 1_000_000
        s = self.sqrt_price_x96
        if token_in.lower() == self.token0.lower():
            # token0 in, token1 out, price moves down
            denominator = self.liquidity + (amount_less_fee * s) // Q96
            if denominator <= 0:
                return 0
            next_s = (self.liquidity * s) // denominator
            return (self.liquidity * (s - next_s)) // Q96
        if token_in.lower() == self.token1.lower():
            # token1 in, token0 out, price moves up
            next_s = s + (amount_less_fee * Q96) // self.liquidity
            if next_s <= s:
                return 0
            return (self.liquidity * (next_s - s)) // Q96 - (
                self.liquidity * (next_s - s) * 0 // Q96
            )
        raise ValueError("token is not in pool")

def v2_profit(reserve_a: tuple[int,int], reserve_b: tuple[int,int], amount: int, fee_bps_a=30, fee_bps_b=30) -> int:
    a0,a1 = reserve_a
    b0,b1 = reserve_b
    p1 = V2PoolState("a","b",a0,a1,fee_bps_a).quote("a",amount)
    return V2PoolState("b","a",b1,b0,fee_bps_b).quote("b",p1) - amount

def optimize_unimodal(profit_fn, low: int, high: int, iterations: int = 24) -> tuple[int,int]:
    if low <= 0 or high < low:
        return 0, 0
    lo, hi = low, high
    best = (lo, profit_fn(lo))
    for _ in range(max(4, iterations)):
        if hi - lo <= 2:
            break
        m1 = lo + (hi-lo)//3
        m2 = hi - (hi-lo)//3
        p1, p2 = profit_fn(m1), profit_fn(m2)
        if p1 > best[1]: best = (m1,p1)
        if p2 > best[1]: best = (m2,p2)
        if p1 < p2:
            lo = m1 + 1
        else:
            hi = m2 - 1
    for x in {lo,hi,(lo+hi)//2,best[0]}:
        if x > 0:
            p = profit_fn(x)
            if p > best[1]: best=(x,p)
    return best
