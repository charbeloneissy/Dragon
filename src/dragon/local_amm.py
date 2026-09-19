from __future__ import annotations

from dataclasses import dataclass

Q96 = 1 << 96
Q128 = 1 << 128
MIN_TICK = -887272
MAX_TICK = 887272
MAX_UINT256 = (1 << 256) - 1


def get_sqrt_ratio_at_tick(tick: int) -> int:
    if tick < MIN_TICK or tick > MAX_TICK:
        raise ValueError("tick out of range")
    t = abs(tick)
    ratio = 0x100000000000000000000000000000000
    constants = (
        (0x1, 0xfffcb933bd6fad37aa2d162d1a594001),
        (0x2, 0xfff97272373d413259a46990580e213a),
        (0x4, 0xfff2e50f5f656932ef12357cf3c7fdcc),
        (0x8, 0xffe5caca7e10e4e61c3624eaa0941cd0),
        (0x10, 0xffcb9843d60f6159c9db58835c926644),
        (0x20, 0xff973b41fa98c081472e6896dfb254c0),
        (0x40, 0xff2ea16466c96a3843ec78b326b52861),
        (0x80, 0xfe5dee046a99a2a811c461f1969c3053),
        (0x100, 0xfcbe86c7900a88aedcffc83b479aa3a4),
        (0x200, 0xf987a7253ac413176f2b074cf7815e54),
        (0x400, 0xf3392b0822b70005940c7a398e4b70f3),
        (0x800, 0xe7159475a2c29b7443b29c7fa6e889d9),
        (0x1000, 0xd097f3bdfd2022b8845ad8f792aa5825),
        (0x2000, 0xa9f746462d870fdf8a65dc1f90e061e5),
        (0x4000, 0x70d869a156d2a1b890bb3df62baf32f7),
        (0x8000, 0x31be135f97d08fd981231505542fcfa6),
        (0x10000, 0x9aa508b5b7a84e1c677de54f3e99bc9),
        (0x20000, 0x5d6af8dedb81196699c329225ee604),
        (0x40000, 0x2216e584f5fa1ea926041bedfe98),
        (0x80000, 0x48a170391f7dc42444e8fa2),
    )
    for bit, multiplier in constants:
        if t & bit:
            ratio = (ratio * multiplier) >> 128
    if tick > 0:
        ratio = MAX_UINT256 // ratio
    return (ratio >> 32) + (1 if ratio & ((1 << 32) - 1) else 0)


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
        if token_in.lower() == self.token0.lower():
            rin, rout = self.reserve0, self.reserve1
        elif token_in.lower() == self.token1.lower():
            rin, rout = self.reserve1, self.reserve0
        else:
            raise ValueError("token is not in pool")
        if rin <= 0 or rout <= 0 or self.fee_bps < 0 or self.fee_bps >= 10000:
            return 0
        effective = amount_in * (10000 - self.fee_bps)
        return (effective * rout) // (rin * 10000 + effective)


@dataclass
class V3PoolState:
    token0: str
    token1: str
    sqrt_price_x96: int
    liquidity: int
    fee_pips: int
    tick: int = 0
    initialized_ticks: dict[int, int] | None = None

    def _next_tick(self, zero_for_one: bool, current_tick: int, ticks: dict[int, int]) -> int | None:
        candidates = [t for t in ticks if t <= current_tick] if zero_for_one else [t for t in ticks if t > current_tick]
        return max(candidates) if candidates else None

    @staticmethod
    def _ceil_div(a: int, b: int) -> int:
        return (a + b - 1) // b

    def quote(self, token_in: str, amount_in: int) -> int:
        if amount_in <= 0 or self.liquidity <= 0 or self.sqrt_price_x96 <= 0:
            return 0
        if self.fee_pips < 0 or self.fee_pips >= 1_000_000:
            return 0
        zero_for_one = token_in.lower() == self.token0.lower()
        if not zero_for_one and token_in.lower() != self.token1.lower():
            raise ValueError("token is not in pool")

        sqrt_p = self.sqrt_price_x96
        liquidity = self.liquidity
        remaining = amount_in
        output = 0
        current_tick = self.tick
        ticks = dict(self.initialized_ticks or {})

        for _ in range(512):
            if remaining <= 0 or liquidity <= 0:
                break
            old_p = sqrt_p
            next_tick = self._next_tick(zero_for_one, current_tick, ticks)
            if next_tick is None:
                target = get_sqrt_ratio_at_tick(MIN_TICK if zero_for_one else MAX_TICK)
            else:
                target = get_sqrt_ratio_at_tick(next_tick)

            if zero_for_one and target >= sqrt_p:
                ticks.pop(next_tick, None) if next_tick is not None else None
                current_tick = next_tick - 1 if next_tick is not None else MIN_TICK
                continue
            if not zero_for_one and target <= sqrt_p:
                ticks.pop(next_tick, None) if next_tick is not None else None
                current_tick = next_tick if next_tick is not None else MAX_TICK
                continue

            usable = remaining * (1_000_000 - self.fee_pips) // 1_000_000
            if usable <= 0:
                break

            if zero_for_one:
                amount0_to_target = self._ceil_div(liquidity * (sqrt_p - target) * Q96, sqrt_p * target)
                amount1_to_target = (liquidity * (sqrt_p - target)) // Q96
            else:
                amount1_to_target = self._ceil_div(liquidity * (target - sqrt_p), Q96)
                amount0_to_target = (liquidity * (target - sqrt_p) * Q96) // (target * sqrt_p)

            if usable >= (amount0_to_target if zero_for_one else amount1_to_target):
                consumed_net = amount0_to_target if zero_for_one else amount1_to_target
                gross = self._ceil_div(consumed_net * 1_000_000, 1_000_000 - self.fee_pips)
                gross = min(gross, remaining)
                remaining -= gross
                output += amount1_to_target if zero_for_one else amount0_to_target
                sqrt_p = target
                if next_tick is not None:
                    liq_net = int(ticks.pop(next_tick, 0))
                    liquidity = liquidity - liq_net if zero_for_one else liquidity + liq_net
                    current_tick = next_tick - 1 if zero_for_one else next_tick
                else:
                    current_tick = MIN_TICK if zero_for_one else MAX_TICK
            else:
                if zero_for_one:
                    next_p = (liquidity * sqrt_p * Q96) // (liquidity * Q96 + usable * sqrt_p)
                    output += (liquidity * (sqrt_p - next_p)) // Q96
                else:
                    next_p = sqrt_p + (usable * Q96) // liquidity
                    output += (liquidity * (next_p - sqrt_p) * Q96) // (next_p * sqrt_p)
                sqrt_p = next_p
                remaining = 0
                break
            if sqrt_p == old_p:
                break

        return max(0, output)

    def quote_without_crossing(self, token_in: str, amount_in: int) -> int:
        return self.quote(token_in, amount_in)


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
