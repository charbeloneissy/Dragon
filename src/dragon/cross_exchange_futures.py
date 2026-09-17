from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import yaml

LOG = logging.getLogger("dragon.cross_exchange")

DEFAULT_EXCHANGES = (
    "binanceusdm", "bybit", "okx", "bitget", "gateio", "kucoinfutures",
    "mexc", "bingx", "bitmart", "phemex", "coinex", "htx", "deribit",
    "cryptocom", "woo",
)
CONFIG_PATH = Path(__file__).resolve().parents[2] / "dragon_live_config.yaml"


def _decimal(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class Settings:
    starting_balance: Decimal = Decimal("5")
    min_profit_usdt: Decimal = Decimal("0.005")
    leverage: int = 1
    quote_age_ms: int = 1000
    pair_skew_ms: int = 500
    poll_ms: int = 250
    leg_timeout_ms: int = 1500
    max_hold_ms: int = 30000
    live: bool = False
    exchanges: tuple[str, ...] = DEFAULT_EXCHANGES
    dynamic_sizing: bool = True
    compound_realized_pnl: bool = True
    include_fees: bool = True
    include_slippage: bool = True
    include_funding: bool = True
    order_type: str = "market"

    @classmethod
    def from_yaml(cls, path: str | Path = CONFIG_PATH) -> "Settings":
        with Path(path).open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        strategy = cfg.get("strategy") or {}
        market = cfg.get("market") or {}
        capital = cfg.get("capital") or {}
        arb = cfg.get("arbitrage") or {}
        position = cfg.get("position") or {}
        execution = cfg.get("execution") or {}
        runtime = cfg.get("runtime") or {}
        exchanges = tuple(str(x).strip() for x in (cfg.get("exchanges") or DEFAULT_EXCHANGES) if str(x).strip())

        if strategy.get("type") != "cross_exchange":
            raise ValueError("config strategy.type must be cross_exchange")
        if strategy.get("triangular", False) or strategy.get("intra_exchange", False):
            raise ValueError("triangular and intra-exchange strategies are disabled")
        if market.get("type") != "futures_only" or market.get("spot", False):
            raise ValueError("config must be futures-only with spot disabled")
        if len(exchanges) != 15:
            raise ValueError("config must contain exactly 15 exchanges")
        if _decimal(arb.get("min_net_profit_usdt", "0.005")) < Decimal("0.005"):
            raise ValueError("minimum net profit cannot be below 0.005 USDT")

        return cls(
            starting_balance=_decimal(capital.get("starting_balance_usdt", "5")),
            min_profit_usdt=_decimal(arb.get("min_net_profit_usdt", "0.005")),
            leverage=max(1, int(market.get("leverage", 1))),
            quote_age_ms=max(50, int(arb.get("max_quote_age_ms", 1000))),
            pair_skew_ms=max(10, int(arb.get("max_pair_skew_ms", 500))),
            poll_ms=max(50, int(runtime.get("poll_interval_ms", 250))),
            leg_timeout_ms=max(250, int(execution.get("leg_timeout_ms", 1500))),
            max_hold_ms=max(1000, int(execution.get("max_hold_ms", 30000))),
            live=str(cfg.get("mode", "paper")).lower() == "live",
            exchanges=exchanges,
            dynamic_sizing=bool(position.get("dynamic_sizing", True)),
            compound_realized_pnl=bool(position.get("compound_realized_pnl", True)),
            include_fees=bool(arb.get("include_trading_fees", True)),
            include_slippage=bool(arb.get("include_slippage", True)),
            include_funding=bool(arb.get("include_funding_cost", True)),
            order_type=str(execution.get("order_type", "market")),
        )

    @classmethod
    def from_env(cls) -> "Settings":
        # Backward-compatible name; operational strategy values now come from YAML.
        return cls.from_yaml()


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
    funding_buffer: Decimal
    net_profit: Decimal
    detected_ms: int


@dataclass
class Position:
    opportunity: Opportunity
    opened_ms: int
    quantity: Decimal


class CrossExchangeFutures:
    """Two-leg cross-exchange perpetual arbitrage only.

    Each position is one long and one short of the same linear perpetual on
    different venues. No triangles and no intra-exchange paths are used.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.from_yaml()
        self.exchange_names = self.settings.exchanges
        self.exchanges: dict[str, Any] = {}
        self.markets: dict[str, dict[str, Any]] = {}
        self.positions: list[Position] = []

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
            if password:
                params["password"] = password
            if uid:
                params["uid"] = uid
        return cls(params)

    async def load(self):
        for name in self.exchange_names:
            try:
                ex = self._make_exchange(name)
                markets = await ex.load_markets()
                swaps = {
                    symbol: market for symbol, market in markets.items()
                    if market.get("swap") and market.get("linear")
                    and market.get("quote") in {"USDT", "USDC"}
                    and market.get("active", True)
                }
                self.exchanges[name] = ex
                self.markets[name] = swaps
                LOG.info("loaded %s: %d linear perpetuals", name, len(swaps))
            except Exception:
                LOG.exception("failed to initialize %s", name)
        if len(self.exchanges) < 2:
            raise RuntimeError("fewer than two futures venues initialized")

    def _symbols_for_pair(self, a: str, b: str) -> set[str]:
        return set(self.markets.get(a, {})).intersection(self.markets.get(b, {}))

    def _fee_rate(self, name: str) -> Decimal:
        raw = os.getenv(f"{name.upper()}_TAKER_FEE_BPS") or os.getenv("DRAGON_DEFAULT_TAKER_FEE_BPS", "5")
        return _decimal(raw) / Decimal("10000")

    @staticmethod
    def _step(market: dict[str, Any]) -> Decimal:
        amount = (market.get("precision") or {}).get("amount")
        if amount is None:
            return Decimal("0.00000001")
        if isinstance(amount, int):
            return Decimal("1e-" + str(amount))
        return _decimal(amount)

    def _minimum_quantity(self, long_name: str, short_name: str, symbol: str, price: Decimal) -> Decimal:
        markets = (self.markets[long_name][symbol], self.markets[short_name][symbol])
        mins: list[Decimal] = []
        for market in markets:
            limits = market.get("limits") or {}
            amount_min = (limits.get("amount") or {}).get("min")
            cost_min = (limits.get("cost") or {}).get("min")
            if amount_min not in (None, 0):
                mins.append(_decimal(amount_min))
            if cost_min not in (None, 0):
                mins.append(_decimal(cost_min) / price)
        minimum = max(mins or [Decimal("0")])
        step = max(self._step(markets[0]), self._step(markets[1]))
        if minimum > 0 and step > 0:
            minimum = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
        return minimum

    async def _quote(self, name: str, symbol: str) -> Quote | None:
        try:
            ticker = await self.exchanges[name].fetch_ticker(symbol)
            bid, ask = ticker.get("bid"), ticker.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                return None
            now = int(time.time() * 1000)
            ts = int(ticker.get("timestamp") or now)
            return Quote(name, symbol, _decimal(bid), _decimal(ask), _decimal(ticker.get("bidVolume") or 0), _decimal(ticker.get("askVolume") or 0), ts)
        except Exception:
            return None

    def _funding_buffer(self, qty: Decimal, mid: Decimal) -> Decimal:
        return mid * qty * _decimal(os.getenv("DRAGON_FUNDING_BUFFER_BPS", "1")) / Decimal("10000")

    def evaluate_pair(self, a: Quote, b: Quote) -> Opportunity | None:
        if a.symbol != b.symbol or a.exchange == b.exchange:
            return None
        now = int(time.time() * 1000)
        if now - a.ts_ms > self.settings.quote_age_ms or now - b.ts_ms > self.settings.quote_age_ms:
            return None
        if abs(a.ts_ms - b.ts_ms) > self.settings.pair_skew_ms or a.ask >= b.bid:
            return None
        minimum = self._minimum_quantity(a.exchange, b.exchange, a.symbol, max(a.ask, b.bid))
        if minimum <= 0:
            return None
        qty = minimum
        if a.ask_qty > 0 and b.bid_qty > 0:
            qty = min(qty, a.ask_qty, b.bid_qty)
        if qty < minimum:
            return None
        gross = (b.bid - a.ask) * qty
        fees = Decimal("0")
        slippage = Decimal("0")
        funding = Decimal("0")
        if self.settings.include_fees:
            fees = a.ask * qty * self._fee_rate(a.exchange) + b.bid * qty * self._fee_rate(b.exchange)
        if self.settings.include_slippage:
            slip_bps = _decimal(os.getenv("DRAGON_SLIPPAGE_BPS", "2"))
            slippage = ((a.ask + b.bid) * qty / Decimal("2")) * slip_bps / Decimal("10000")
        if self.settings.include_funding:
            funding = self._funding_buffer(qty, (a.ask + b.bid) / Decimal("2"))
        net = gross - fees - slippage - funding
        if net < self.settings.min_profit_usdt:
            return None
        return Opportunity(a.symbol, a.exchange, b.exchange, a.ask, b.bid, qty, gross, fees, slippage, funding, net, now)

    async def scan_once(self) -> list[Opportunity]:
        rows: list[Opportunity] = []
        names = list(self.exchanges)
        for i, left_name in enumerate(names):
            for right_name in names[i + 1:]:
                for symbol in self._symbols_for_pair(left_name, right_name):
                    left, right = await asyncio.gather(self._quote(left_name, symbol), self._quote(right_name, symbol))
                    if left is None or right is None:
                        continue
                    for a, b in ((left, right), (right, left)):
                        op = self.evaluate_pair(a, b)
                        if op:
                            rows.append(op)
        rows.sort(key=lambda x: x.net_profit, reverse=True)
        return rows

    async def _free_margin(self, name: str) -> Decimal:
        balance = await self.exchanges[name].fetch_balance({"type": "swap"})
        free = balance.get("free") or {}
        return _decimal(free.get("USDT", 0) or 0)

    async def _compound_quantity(self, op: Opportunity) -> Decimal:
        if not self.settings.dynamic_sizing:
            return op.quantity
        long_free, short_free = await asyncio.gather(self._free_margin(op.long_exchange), self._free_margin(op.short_exchange))
        if min(long_free, short_free) <= 0:
            return Decimal("0")
        if not self.settings.compound_realized_pnl:
            return op.quantity
        units = max(1, int(min(long_free, short_free) / self.settings.starting_balance))
        return op.quantity * Decimal(units)

    async def execute(self, op: Opportunity) -> dict[str, Any]:
        if not self.settings.live:
            return {"status": "paper", "opportunity": op.__dict__}

        qty = await self._compound_quantity(op)
        if qty <= 0:
            return {"status": "rejected", "reason": "insufficient_free_margin"}
        long_ex = self.exchanges[op.long_exchange]
        short_ex = self.exchanges[op.short_exchange]
        try:
            qty_long = float(long_ex.amount_to_precision(op.symbol, float(qty)))
            qty_short = float(short_ex.amount_to_precision(op.symbol, float(qty)))
        except Exception:
            return {"status": "rejected", "reason": "invalid_exchange_quantity"}
        if qty_long <= 0 or qty_short <= 0:
            return {"status": "rejected", "reason": "below_exchange_minimum"}
        qty_f = min(qty_long, qty_short)
        await long_ex.set_leverage(self.settings.leverage, op.symbol)
        await short_ex.set_leverage(self.settings.leverage, op.symbol)
        first = second = None
        try:
            first, second = await asyncio.gather(
                asyncio.wait_for(long_ex.create_order(op.symbol, self.settings.order_type, "buy", qty_f, None, {"reduceOnly": False}), self.settings.leg_timeout_ms / 1000),
                asyncio.wait_for(short_ex.create_order(op.symbol, self.settings.order_type, "sell", qty_f, None, {"reduceOnly": False}), self.settings.leg_timeout_ms / 1000),
            )
            self.positions.append(Position(op, int(time.time() * 1000), _decimal(qty_f)))
            return {"status": "opened", "long": first, "short": second, "quantity": str(qty_f)}
        except Exception as exc:
            LOG.exception("paired execution failed")
            for ex, order, side in ((long_ex, first, "sell"), (short_ex, second, "buy")):
                if order:
                    try:
                        filled = _decimal(order.get("filled") or qty_f)
                        if filled > 0:
                            await ex.create_order(op.symbol, "market", side, float(filled), None, {"reduceOnly": True})
                    except Exception:
                        LOG.exception("failed to flatten filled leg")
            return {"status": "hedge_failed", "error": str(exc)}

    async def close_position(self, position: Position) -> bool:
        op = position.opportunity
        long_ex, short_ex = self.exchanges[op.long_exchange], self.exchanges[op.short_exchange]
        qty = float(position.quantity)
        try:
            await asyncio.gather(
                asyncio.wait_for(long_ex.create_order(op.symbol, "market", "sell", qty, None, {"reduceOnly": True}), self.settings.leg_timeout_ms / 1000),
                asyncio.wait_for(short_ex.create_order(op.symbol, "market", "buy", qty, None, {"reduceOnly": True}), self.settings.leg_timeout_ms / 1000),
            )
            return True
        except Exception:
            LOG.exception("paired close failed for %s", op.symbol)
            return False

    async def manage_positions(self):
        if not self.positions:
            return
        now = int(time.time() * 1000)
        remaining: list[Position] = []
        for position in self.positions:
            op = position.opportunity
            left, right = await asyncio.gather(self._quote(op.long_exchange, op.symbol), self._quote(op.short_exchange, op.symbol))
            close = now - position.opened_ms >= self.settings.max_hold_ms
            if left and right and left.bid >= right.ask:
                close = True
            if close and await self.close_position(position):
                LOG.info("CLOSED %s", op.symbol)
            else:
                remaining.append(position)
        self.positions = remaining

    async def close(self):
        await asyncio.gather(*(ex.close() for ex in self.exchanges.values()), return_exceptions=True)


async def run() -> None:
    logging.basicConfig(level=logging.INFO)
    engine = CrossExchangeFutures()
    await engine.load()
    try:
        while True:
            await engine.manage_positions()
            for op in await engine.scan_once():
                await engine.execute(op)
            await asyncio.sleep(engine.settings.poll_ms / 1000)
    finally:
        await engine.close()


if __name__ == "__main__":
    asyncio.run(run())
