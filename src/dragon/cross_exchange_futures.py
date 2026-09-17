from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

import ccxt.async_support as ccxt

LOG = logging.getLogger("dragon.cross_exchange")

DEFAULT_EXCHANGES = (
    "binanceusdm", "bybit", "okx", "bitget", "gateio", "kucoinfutures",
    "mexc", "bingx", "bitmart", "phemex", "coinex", "htx", "deribit",
    "cryptocom", "woo",
)


@dataclass(frozen=True)
class Settings:
    starting_balance: Decimal = Decimal("5")
    min_profit_usdt: Decimal = Decimal("0.005")
    leverage: int = 1
    quote_age_ms: int = 1000
    pair_skew_ms: int = 500
    poll_ms: int = 250
    leg_timeout_ms: int = 1500
    live: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            starting_balance=Decimal(os.getenv("DRAGON_STARTING_BALANCE_USDT", "5")),
            min_profit_usdt=Decimal(os.getenv("DRAGON_MIN_NET_PROFIT_USDT", "0.005")),
            leverage=max(1, int(os.getenv("DRAGON_LEVERAGE", "1"))),
            quote_age_ms=max(50, int(os.getenv("DRAGON_MAX_QUOTE_AGE_MS", "1000"))),
            pair_skew_ms=max(10, int(os.getenv("DRAGON_MAX_PAIR_SKEW_MS", "500"))),
            poll_ms=max(50, int(os.getenv("DRAGON_POLL_MS", "250"))),
            leg_timeout_ms=max(250, int(os.getenv("DRAGON_LEG_TIMEOUT_MS", "1500"))),
            live=os.getenv("DRAGON_LIVE_TRADING", "false").lower() in {"1", "true", "yes", "on"},
        )


@dataclass
class Quote:
    exchange: str
    symbol: str
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    ts_ms: int


@dataclass
class Opportunity:
    symbol: str
    long_exchange: str
    short_exchange: str
    long_price: Decimal
    short_price: Decimal
    quantity: Decimal
    gross_profit: Decimal
    fees: Decimal
    slippage: Decimal
    net_profit: Decimal
    detected_ms: int


class CrossExchangeFutures:
    """Cross-exchange perpetual futures engine.

    It deliberately has no triangle/path logic. Every opportunity is exactly
    two legs on the same contract: long the cheaper venue and short the more
    expensive venue. Live trading is opt-in and remains disabled by default.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_env()
        names = os.getenv("DRAGON_EXCHANGES", ",".join(DEFAULT_EXCHANGES))
        self.exchange_names = tuple(x.strip() for x in names.split(",") if x.strip())
        if len(self.exchange_names) != 15:
            raise ValueError("DRAGON_EXCHANGES must contain exactly 15 exchanges")
        self.exchanges: dict[str, Any] = {}
        self.markets: dict[str, dict[str, Any]] = {}
        self.last_quotes: dict[tuple[str, str], Quote] = {}

    def _make_exchange(self, name: str):
        cls = getattr(ccxt, name, None)
        if cls is None:
            raise ValueError(f"ccxt exchange is unavailable: {name}")
        params: dict[str, Any] = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        prefix = name.upper()
        key = os.getenv(f"{prefix}_API_KEY", "").strip()
        secret = os.getenv(f"{prefix}_API_SECRET", "").strip()
        password = os.getenv(f"{prefix}_API_PASSWORD", "").strip()
        uid = os.getenv(f"{prefix}_API_UID", "").strip()
        if key and secret:
            params["apiKey"], params["secret"] = key, secret
            if password: params["password"] = password
            if uid: params["uid"] = uid
        return cls(params)

    async def load(self):
        for name in self.exchange_names:
            try:
                ex = self._make_exchange(name)
                markets = await ex.load_markets()
                swaps = {
                    symbol: market for symbol, market in markets.items()
                    if market.get("swap") and market.get("linear") and market.get("quote") in {"USDT", "USDC"}
                    and market.get("active", True)
                }
                self.exchanges[name] = ex
                self.markets[name] = swaps
                LOG.info("loaded %s: %d linear perpetuals", name, len(swaps))
            except Exception:
                LOG.exception("failed to initialize %s", name)
        if len(self.exchanges) < 2:
            raise RuntimeError("fewer than two futures venues initialized")

    def common_symbols(self) -> list[str]:
        sets = [set(m) for m in self.markets.values()]
        if not sets:
            return []
        return sorted(set.intersection(*sets))

    def _fee_rate(self, name: str) -> Decimal:
        raw = os.getenv(f"{name.upper()}_TAKER_FEE_BPS", "")
        if raw:
            return Decimal(raw) / Decimal("10000")
        return Decimal(os.getenv("DRAGON_DEFAULT_TAKER_FEE_BPS", "5")) / Decimal("10000")

    @staticmethod
    def _market_min(market: dict[str, Any]) -> Decimal:
        limits = market.get("limits") or {}
        amount_min = (limits.get("amount") or {}).get("min")
        cost_min = (limits.get("cost") or {}).get("min")
        values = [Decimal(str(x)) for x in (amount_min, cost_min) if x not in (None, 0)]
        return max(values, default=Decimal("0"))

    @staticmethod
    def _step(market: dict[str, Any]) -> Decimal:
        precision = market.get("precision") or {}
        amount = precision.get("amount")
        if amount is None:
            return Decimal("0.00000001")
        return Decimal("1e-" + str(amount)) if isinstance(amount, int) else Decimal(str(amount))

    def _minimum_quantity(self, long_name: str, short_name: str, symbol: str, price: Decimal) -> Decimal:
        a = self.markets[long_name][symbol]
        b = self.markets[short_name][symbol]
        # CCXT expresses amount limits in base-contract units. Prefer the
        # exchange minimum amount; cost minimums are converted using price.
        mins = []
        for market in (a, b):
            limits = market.get("limits") or {}
            amin = (limits.get("amount") or {}).get("min")
            cmin = (limits.get("cost") or {}).get("min")
            if amin not in (None, 0): mins.append(Decimal(str(amin)))
            if cmin not in (None, 0): mins.append(Decimal(str(cmin)) / price)
        qty = max(mins or [Decimal("0")])
        step = max(self._step(a), self._step(b))
        if step > 0:
            qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        return qty

    async def _quote(self, name: str, symbol: str) -> Quote | None:
        ex = self.exchanges[name]
        try:
            t = await ex.fetch_ticker(symbol)
            bid, ask = t.get("bid"), t.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                return None
            now = int(time.time() * 1000)
            ts = int(t.get("timestamp") or now)
            return Quote(name, symbol, Decimal(str(bid)), Decimal(str(ask)), Decimal(str(t.get("bidVolume") or 0)), Decimal(str(t.get("askVolume") or 0)), ts)
        except Exception:
            return None

    def evaluate_pair(self, a: Quote, b: Quote) -> Opportunity | None:
        # Direction 1: long A / short B. Direction 2 is evaluated separately.
        if a.symbol != b.symbol or a.exchange == b.exchange:
            return None
        now = int(time.time() * 1000)
        if now - a.ts_ms > self.settings.quote_age_ms or now - b.ts_ms > self.settings.quote_age_ms:
            return None
        if abs(a.ts_ms - b.ts_ms) > self.settings.pair_skew_ms:
            return None
        if a.ask >= b.bid:
            return None
        qty = self._minimum_quantity(a.exchange, b.exchange, a.symbol, max(a.ask, b.bid))
        if qty <= 0:
            return None
        if a.ask_qty > 0 and b.bid_qty > 0:
            qty = min(qty, a.ask_qty, b.bid_qty)
        if qty <= 0:
            return None
        gross = (b.bid - a.ask) * qty
        fee = (a.ask * qty * self._fee_rate(a.exchange)) + (b.bid * qty * self._fee_rate(b.exchange))
        # A conservative execution buffer; book depth beyond top-of-book is
        # intentionally not assumed when only ticker quotes are available.
        slip_bps = Decimal(os.getenv("DRAGON_SLIPPAGE_BPS", "2"))
        slippage = ((a.ask + b.bid) * qty / Decimal("2")) * slip_bps / Decimal("10000")
        net = gross - fee - slippage
        if net < self.settings.min_profit_usdt:
            return None
        return Opportunity(a.symbol, a.exchange, b.exchange, a.ask, b.bid, qty, gross, fee, slippage, net, now)

    async def scan_once(self) -> list[Opportunity]:
        symbols = self.common_symbols()
        rows: list[Opportunity] = []
        # Ticker requests are concurrent but bounded by the exchange's own
        # rate limiter. No artificial trade-count limit is applied.
        for symbol in symbols:
            quotes = await asyncio.gather(*(self._quote(n, symbol) for n in self.exchanges))
            valid = [q for q in quotes if q is not None]
            for i, left in enumerate(valid):
                for right in valid[i + 1:]:
                    op = self.evaluate_pair(left, right)
                    if op:
                        rows.append(op)
                    # Reverse direction without duplicating market-data calls.
                    op = self.evaluate_pair(right, left)
                    if op:
                        rows.append(op)
        rows.sort(key=lambda x: x.net_profit, reverse=True)
        return rows

    async def _free_margin(self, name: str) -> Decimal:
        ex = self.exchanges[name]
        balance = await ex.fetch_balance({"type": "swap"})
        free = balance.get("free") or {}
        return Decimal(str(free.get("USDT", 0) or 0))

    async def execute(self, op: Opportunity) -> dict[str, Any]:
        if not self.settings.live:
            return {"status": "paper", "opportunity": op.__dict__}
        long_ex = self.exchanges[op.long_exchange]
        short_ex = self.exchanges[op.short_exchange]
        qty = float(op.quantity)
        await long_ex.set_leverage(self.settings.leverage, op.symbol)
        await short_ex.set_leverage(self.settings.leverage, op.symbol)
        # Open both legs as close together as the APIs permit. If one leg
        # fails, immediately attempt to flatten the successful leg.
        first = second = None
        try:
            first, second = await asyncio.gather(
                asyncio.wait_for(long_ex.create_order(op.symbol, "market", "buy", qty, None, {"reduceOnly": False}), self.settings.leg_timeout_ms / 1000),
                asyncio.wait_for(short_ex.create_order(op.symbol, "market", "sell", qty, None, {"reduceOnly": False}), self.settings.leg_timeout_ms / 1000),
            )
            return {"status": "opened", "long": first, "short": second, "opportunity": op.__dict__}
        except Exception as exc:
            LOG.exception("paired execution failed")
            if first:
                try: await long_ex.create_order(op.symbol, "market", "sell", qty, None, {"reduceOnly": True})
                except Exception: LOG.exception("failed to flatten long leg")
            if second:
                try: await short_ex.create_order(op.symbol, "market", "buy", qty, None, {"reduceOnly": True})
                except Exception: LOG.exception("failed to flatten short leg")
            return {"status": "hedge_failed", "error": str(exc), "opportunity": op.__dict__}

    async def close(self):
        await asyncio.gather(*(ex.close() for ex in self.exchanges.values()), return_exceptions=True)


async def run() -> None:
    logging.basicConfig(level=os.getenv("DRAGON_LOG_LEVEL", "INFO"))
    engine = CrossExchangeFutures()
    await engine.load()
    try:
        while True:
            opportunities = await engine.scan_once()
            for op in opportunities:
                LOG.info("ARB %s long=%s short=%s qty=%s net=$%s", op.symbol, op.long_exchange, op.short_exchange, op.quantity, op.net_profit)
                await engine.execute(op)
            await asyncio.sleep(engine.settings.poll_ms / 1000)
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(run())
