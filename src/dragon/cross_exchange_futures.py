from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt
import yaml

LOG = logging.getLogger("dragon.cross_exchange")
DEFAULT_EXCHANGES = ("binanceusdm", "bybit", "okx", "bitget", "gateio", "kucoinfutures", "mexc", "bingx", "bitmart", "phemex", "coinex", "htx", "deribit", "cryptocom", "woo")
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
        strategy, market, capital = cfg.get("strategy") or {}, cfg.get("market") or {}, cfg.get("capital") or {}
        arb, position = cfg.get("arbitrage") or {}, cfg.get("position") or {}
        execution, runtime = cfg.get("execution") or {}, cfg.get("runtime") or {}
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
    """Two-leg cross-exchange perpetual arbitrage only. No triangles or intra-exchange paths."""
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
        key, secret = os.getenv(f"{prefix}_API_KEY", "").strip(), os.getenv(f"{prefix}_API_SECRET", "").strip()
        password, uid = os.getenv(f"{prefix}_API_PASSWORD", "").strip(), os.getenv(f"{prefix}_API_UID", "").strip()
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
                swaps = {s: m for s, m in markets.items() if m.get("swap") and m.get("linear") and m.get("quote") in {"USDT", "USDC"} and m.get("active", True)}
                self.exchanges[name], self.markets[name] = ex, swaps
                LOG.info("loaded %s: %d linear perpetuals", name, len(swaps))
            except Exception:
                LOG.exception("failed to initialize %s", name)
        if len(self.exchanges) < 2: raise RuntimeError("fewer than two futures venues initialized")

    def _symbols_for_pair(self, a: str, b: str) -> set[str]:
        return set(self.markets.get(a, {})).intersection(self.markets.get(b, {}))

    def _fee_rate(self, name: str) -> Decimal:
        raw = os.getenv(f"{name.upper()}_TAKER_FEE_BPS") or os.getenv("DRAGON_DEFAULT_TAKER_FEE_BPS", "5")
        return _decimal(raw) / Decimal("10000")

    @staticmethod
    def _step(market: dict[str, Any]) -> Decimal:
        amount = (market.get("precision") or {}).get("amount")
        if amount is None: return Decimal("0.00000001")
        if isinstance(amount, int): return Decimal("1e-" + str(amount))
        return _decimal(amount)

    def _minimum_quantity(self, long_name: str, short_name: str, symbol: str, price: Decimal) -> Decimal:
        markets = (self.markets[long_name][symbol], self.markets[short_name][symbol])
        mins: list[Decimal] = []
        for market in markets:
            limits = market.get("limits") or {}
            amount_min = (limits.get("amount") or {}).get("min")
            cost_min = (limits.get("cost") or {}).get("min")
            if amount_min not in (None, 0): mins.append(_decimal(amount_min))
            if cost_min not in (None, 0): mins.append(_decimal(cost_min) / price)
        minimum = max(mins or [Decimal("0")])
        step = max(self._step(markets[0]), self._step(markets[1]))
        if minimum > 0 and step > 0: minimum = (minimum / step).to_integral_value(rounding=ROUND_CEILING) * step
        return minimum

    def _common_step(self, long_name: str, short_name: str, symbol: str) -> Decimal:
        return max(self._step(self.markets[long_name][symbol]), self._step(self.markets[short_name][symbol]))

    def _round_down_common(self, qty: Decimal, long_name: str, short_name: str, symbol: str) -> Decimal:
        step = self._common_step(long_name, short_name, symbol)
        return (qty / step).to_integral_value(rounding=ROUND_FLOOR) * step if step > 0 else qty

    def _profit_for_quantity(self, op: Opportunity, qty: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal]:
        gross = (op.short_price - op.long_price) * qty
        fees = op.long_price * qty * self._fee_rate(op.long_exchange) + op.short_price * qty * self._fee_rate(op.short_exchange) if self.settings.include_fees else Decimal("0")
        slippage = ((op.long_price + op.short_price) * qty / Decimal("2")) * _decimal(os.getenv("DRAGON_SLIPPAGE_BPS", "2")) / Decimal("10000") if self.settings.include_slippage else Decimal("0")
        funding = self._funding_buffer(qty, (op.long_price + op.short_price) / Decimal("2")) if self.settings.include_funding else Decimal("0")
        return gross, fees, slippage, funding, gross - fees - slippage - funding

    async def _quote(self, name: str, symbol: str) -> Quote | None:
        try:
            ticker = await self.exchanges[name].fetch_ticker(symbol)
            bid, ask = ticker.get("bid"), ticker.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid: return None
            now = int(time.time() * 1000)
            ts = int(ticker.get("timestamp") or now)
            return Quote(name, symbol, _decimal(bid), _decimal(ask), _decimal(ticker.get("bidVolume") or 0), _decimal(ticker.get("askVolume") or 0), ts)
        except Exception:
            return None

    def _funding_buffer(self, qty: Decimal, mid: Decimal) -> Decimal:
        return mid * qty * _decimal(os.getenv("DRAGON_FUNDING_BUFFER_BPS", "1")) / Decimal("10000")

    def evaluate_pair(self, a: Quote, b: Quote) -> Opportunity | None:
        if a.symbol != b.symbol or a.exchange == b.exchange: return None
        now = int(time.time() * 1000)
        if now - a.ts_ms > self.settings.quote_age_ms or now - b.ts_ms > self.settings.quote_age_ms: return None
        if abs(a.ts_ms - b.ts_ms) > self.settings.pair_skew_ms or a.ask >= b.bid: return None
        minimum = self._minimum_quantity(a.exchange, b.exchange, a.symbol, max(a.ask, b.bid))
        if minimum <= 0: return None
        qty = minimum
        if a.ask_qty > 0 and b.bid_qty > 0: qty = min(qty, a.ask_qty, b.bid_qty)
        if qty < minimum: return None
        seed = Opportunity(a.symbol, a.exchange, b.exchange, a.ask, b.bid, qty, Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0), now)
        gross, fees, slippage, funding, net = self._profit_for_quantity(seed, qty)
        if net < self.settings.min_profit_usdt: return None
        return Opportunity(a.symbol, a.exchange, b.exchange, a.ask, b.bid, qty, gross, fees, slippage, funding, net, now)

    async def scan_once(self) -> list[Opportunity]:
        rows: list[Opportunity] = []
        names = list(self.exchanges)
        for i, left_name in enumerate(names):
            for right_name in names[i + 1:]:
                for symbol in self._symbols_for_pair(left_name, right_name):
                    left, right = await asyncio.gather(self._quote(left_name, symbol), self._quote(right_name, symbol))
                    if left is None or right is None: continue
                    for a, b in ((left, right), (right, left)):
                        op = self.evaluate_pair(a, b)
                        if op: rows.append(op)
        rows.sort(key=lambda x: x.net_profit, reverse=True)
        return rows

    async def _free_margin(self, name: str) -> Decimal:
        balance = await self.exchanges[name].fetch_balance({"type": "swap"})
        free = balance.get("free") or {}
        return _decimal(free.get("USDT", 0) or 0)

    async def _execution_quantity(self, op: Opportunity) -> Decimal:
        minimum = self._minimum_quantity(op.long_exchange, op.short_exchange, op.symbol, max(op.long_price, op.short_price))
        long_free, short_free = await asyncio.gather(self._free_margin(op.long_exchange), self._free_margin(op.short_exchange))
        if min(long_free, short_free) <= 0: return Decimal("0")
        if not self.settings.dynamic_sizing or not self.settings.compound_realized_pnl:
            target = op.quantity
        else:
            # Available margin is the source of truth for compounding. No capital or trade-count cap is applied.
            max_notional = min(long_free, short_free) * Decimal(self.settings.leverage)
            target = max_notional / max(op.long_price, op.short_price)
        target = self._round_down_common(target, op.long_exchange, op.short_exchange, op.symbol)
        return target if target >= minimum else Decimal("0")

    async def execute(self, op: Opportunity) -> dict[str, Any]:
        if not self.settings.live: return {"status": "paper", "opportunity": op.__dict__}
        qty = await self._execution_quantity(op)
        if qty <= 0: return {"status": "rejected", "reason": "insufficient_free_margin_or_below_minimum"}
        gross, fees, slippage, funding, net = self._profit_for_quantity(op, qty)
        if net < self.settings.min_profit_usdt: return {"status": "rejected", "reason": "net_profit_below_threshold", "net_profit": str(net)}
        long_ex, short_ex = self.exchanges[op.long_exchange], self.exchanges[op.short_exchange]
        try:
            qty_long = _decimal(long_ex.amount_to_precision(op.symbol, float(qty)))
            qty_short = _decimal(short_ex.amount_to_precision(op.symbol, float(qty)))
        except Exception: return {"status": "rejected", "reason": "invalid_exchange_quantity"}
        qty_f = min(qty_long, qty_short)
        minimum = self._minimum_quantity(op.long_exchange, op.short_exchange, op.symbol, max(op.long_price, op.short_price))
        if qty_f < minimum or qty_f <= 0: return {"status": "rejected", "reason": "below_exchange_minimum"}
        try:
            await asyncio.gather(long_ex.set_leverage(self.settings.leverage, op.symbol), short_ex.set_leverage(self.settings.leverage, op.symbol))
        except Exception as exc: return {"status": "rejected", "reason": "leverage_setup_failed", "error": str(exc)}
        first = second = None
        try:
            first, second = await asyncio.gather(
                asyncio.wait_for(long_ex.create_order(op.symbol, self.settings.order_type, "buy", float(qty_f), None, {"reduceOnly": False}), self.settings.leg_timeout_ms / 1000),
                asyncio.wait_for(short_ex.create_order(op.symbol, self.settings.order_type, "sell", float(qty_f), None, {"reduceOnly": False}), self.settings.leg_timeout_ms / 1000),
            )
            self.positions.append(Position(op, int(time.time() * 1000), qty_f))
            return {"status": "opened", "long": first, "short": second, "quantity": str(qty_f), "net_profit_estimate": str(net)}
        except Exception as exc:
            LOG.exception("paired execution failed")
            for ex, order, side in ((long_ex, first, "sell"), (short_ex, second, "buy")):
                if order:
                    try:
                        filled_raw = order.get("filled")
                        filled = _decimal(filled_raw) if filled_raw is not None else Decimal("0")
                        if filled > 0: await ex.create_order(op.symbol, "market", side, float(filled), None, {"reduceOnly": True})
                    except Exception: LOG.exception("failed to flatten filled leg")
            return {"status": "hedge_failed", "error": str(exc)}

    async def close_position(self, position: Position) -> bool:
        op = position.opportunity
        long_ex, short_ex = self.exchanges[op.long_exchange], self.exchanges[op.short_exchange]
        try:
            await asyncio.gather(
                asyncio.wait_for(long_ex.create_order(op.symbol, "market", "sell", float(position.quantity), None, {"reduceOnly": True}), self.settings.leg_timeout_ms / 1000),
                asyncio.wait_for(short_ex.create_order(op.symbol, "market", "buy", float(position.quantity), None, {"reduceOnly": True}), self.settings.leg_timeout_ms / 1000),
            )
            return True
        except Exception:
            LOG.exception("paired close failed for %s", op.symbol)
            return False

    async def manage_positions(self):
        if not self.positions: return
        now = int(time.time() * 1000)
        remaining: list[Position] = []
        for position in self.positions:
            op = position.opportunity
            left, right = await asyncio.gather(self._quote(op.long_exchange, op.symbol), self._quote(op.short_exchange, op.symbol))
            close = now - position.opened_ms >= self.settings.max_hold_ms
            if left and right and left.bid >= right.ask: close = True
            if close and await self.close_position(position): LOG.info("CLOSED %s", op.symbol)
            else: remaining.append(position)
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
            for op in await engine.scan_once(): await engine.execute(op)
            await asyncio.sleep(engine.settings.poll_ms / 1000)
    finally:
        await engine.close()
