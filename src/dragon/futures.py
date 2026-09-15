from __future__ import annotations

import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN
from typing import List, Tuple
from urllib.parse import urlencode

import httpx


class FuturesError(RuntimeError):
    pass


def _floor(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class FuturesClient:
    """USDⓈ-M Futures client with rate-limit aware reads and non-retrying live writes."""
    def __init__(self, api_base="https://fapi.binance.com", api_key="", api_secret="", timeout=5.0):
        self.base = api_base.rstrip("/")
        self.key = api_key.strip()
        self.secret = api_secret.strip()
        self.offset_ms = 0
        self.http = httpx.Client(timeout=timeout, limits=httpx.Limits(max_connections=10, max_keepalive_connections=5))
        self._next_allowed = 0.0
        self._backoff = 1.0

    def close(self):
        self.http.close()

    def _throttle(self):
        delay = self._next_allowed - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, 300.0))

    def _response_error(self, response, label):
        if response.status_code in (418, 429):
            retry = response.headers.get("Retry-After")
            try:
                wait = max(float(retry), self._backoff) if retry else self._backoff
            except ValueError:
                wait = self._backoff
            self._next_allowed = time.monotonic() + min(wait, 86400.0)
            self._backoff = min(max(self._backoff * 2.0, wait), 300.0)
            raise FuturesError(f"{label}: Binance rate limit HTTP {response.status_code}; backing off {wait:.1f}s")
        if response.status_code >= 400:
            try:
                body = response.json()
                raise FuturesError(body.get("msg", response.text[:500]))
            except ValueError:
                raise FuturesError(response.text[:500])

    def public(self, path, params=None):
        self._throttle()
        try:
            r = self.http.get(self.base + path, params=params or {})
            if r.status_code < 400:
                self._backoff = 1.0
                return r.json()
            self._response_error(r, f"futures public request {path}")
        except FuturesError:
            raise
        except Exception as exc:
            raise FuturesError(f"futures public request failed: {exc}") from exc

    def sync_time(self):
        before = int(time.time() * 1000)
        server = self.public("/fapi/v1/time")["serverTime"]
        after = int(time.time() * 1000)
        self.offset_ms = int(server - ((before + after) // 2))
        return self.offset_ms

    def signed(self, method, path, params=None):
        if not self.key or not self.secret:
            raise FuturesError("Futures credentials missing")
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["timestamp"] = int(time.time() * 1000) + self.offset_ms
        p.setdefault("recvWindow", 5000)
        query = urlencode(p, doseq=True)
        sig = hmac.new(self.secret.encode("ascii"), query.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": self.key, "Content-Type": "application/x-www-form-urlencoded"}
        self._throttle()
        try:
            if method.upper() == "GET":
                r = self.http.get(self.base + path + "?" + query + "&signature=" + sig, headers=headers)
            else:
                wire = query + "&signature=" + sig
                r = self.http.request(method.upper(), self.base + path, content=wire, headers=headers)
            self._response_error(r, f"futures signed request {path}")
            self._backoff = 1.0
            return r.json()
        except FuturesError:
            raise
        except Exception as exc:
            raise FuturesError(f"futures signed request failed: {exc}") from exc

    def exchange_info(self):
        return self.public("/fapi/v1/exchangeInfo")

    def book_ticker(self, symbol=None):
        return self.public("/fapi/v1/ticker/bookTicker", {"symbol": symbol} if symbol else None)

    def all_book_tickers(self):
        return self.book_ticker()

    def mark_price(self, symbol=None):
        return self.public("/fapi/v1/premiumIndex", {"symbol": symbol} if symbol else None)

    def all_mark_prices(self):
        return self.mark_price()

    def account(self):
        return self.signed("GET", "/fapi/v2/account")

    def position_risk(self, symbol=None):
        return self.signed("GET", "/fapi/v2/positionRisk", {"symbol": symbol} if symbol else None)

    def set_leverage(self, symbol, leverage):
        leverage = max(1, min(2, int(leverage)))
        return self.signed("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage})

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        return self.signed("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": margin_type})

    def order(self, symbol, side, quantity, *, reduce_only=False, position_side=None):
        params = {
            "symbol": symbol, "side": side.upper(), "type": "MARKET",
            "quantity": format(quantity, "f"), "newOrderRespType": "RESULT",
            "reduceOnly": "true" if reduce_only else "false",
            "positionSide": position_side,
        }
        return self.signed("POST", "/fapi/v1/order", params)

    def get_order(self, symbol, order_id):
        return self.signed("GET", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def cancel_order(self, symbol, order_id):
        return self.signed("DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id})

    def create_listen_key(self):
        return self._listen_key("POST")

    def _listen_key(self, method):
        headers = {"X-MBX-APIKEY": self.key}
        self._throttle()
        try:
            r = self.http.request(method, self.base + "/fapi/v1/listenKey", headers=headers)
            self._response_error(r, f"futures listenKey {method}")
            return r.json()["listenKey"]
        except FuturesError:
            raise
        except Exception as exc:
            raise FuturesError(f"futures listenKey request failed: {exc}") from exc

    def keepalive_listen_key(self, listen_key):
        return self._listen_key_action("PUT", listen_key)

    def close_listen_key(self, listen_key):
        return self._listen_key_action("DELETE", listen_key)

    def _listen_key_action(self, method, listen_key):
        headers = {"X-MBX-APIKEY": self.key}
        self._throttle()
        r = self.http.request(method, self.base + "/fapi/v1/listenKey", params={"listenKey": listen_key}, headers=headers)
        self._response_error(r, f"futures listenKey {method}")
        return r.json() if r.text else {}


class FuturesOpportunity:
    def __init__(self, symbol, spot_bid, spot_ask, futures_bid, futures_ask, fee_bps, funding_bps=Decimal("0")):
        self.symbol = symbol
        self.spot_bid = spot_bid
        self.spot_ask = spot_ask
        self.futures_bid = futures_bid
        self.futures_ask = futures_ask
        self.fee_bps = fee_bps
        self.funding_bps = funding_bps

    def cash_and_carry_bps(self):
        if self.spot_ask <= 0 or self.futures_bid <= 0:
            return Decimal("-999999")
        gross = (self.futures_bid / self.spot_ask - Decimal("1")) * Decimal("10000")
        return gross - self.fee_bps * Decimal("2") - self.funding_bps


def evaluate_basis(rows: List[dict], fee_bps: Decimal, min_edge_bps: Decimal) -> List[Tuple[str, Decimal]]:
    opportunities = []
    for row in rows:
        try:
            opp = FuturesOpportunity(
                row["symbol"], Decimal(str(row["spotBid"])), Decimal(str(row["spotAsk"])),
                Decimal(str(row["futuresBid"])), Decimal(str(row["futuresAsk"])), fee_bps,
                Decimal(str(row.get("fundingBps", "0"))),
            )
            edge = opp.cash_and_carry_bps()
            if edge >= min_edge_bps:
                opportunities.append((opp.symbol, edge))
        except (KeyError, ValueError, ArithmeticError):
            continue
    return sorted(opportunities, key=lambda x: x[1], reverse=True)


class BasisHedgeEngine:
    """Matched spot-long / perpetual-short hedge with immediate unwind on leg failure."""
    def __init__(self, futures, spot, max_notional_usdt=25, leverage=1, live=False):
        self.futures = futures
        self.spot = spot
        self.max_notional = Decimal(str(max_notional_usdt))
        self.leverage = max(1, min(2, int(leverage)))
        self.live = bool(live)

    def open_hedge(self, symbol, quantity, spot_price, edge_bps):
        qty = Decimal(str(quantity))
        price = Decimal(str(spot_price))
        notional = qty * price
        if not self.live:
            return {"live": False, "symbol": symbol, "quantity": str(qty), "edge_bps": str(edge_bps)}
        if qty <= 0 or notional <= 0 or notional > self.max_notional:
            raise FuturesError("hedge notional/quantity violates safety limit")
        self.futures.set_margin_type(symbol, "ISOLATED")
        self.futures.set_leverage(symbol, self.leverage)
        spot_filled = False
        try:
            spot = self.spot.new_market_order(symbol, "BUY", quantity=qty)
            if spot.get("status") != "FILLED":
                raise FuturesError(f"spot hedge leg not filled: {spot.get('status')}")
            spot_filled = True
            fut = self.futures.order(symbol, "SELL", qty)
            if fut.get("status") != "FILLED":
                raise FuturesError(f"futures hedge leg not filled: {fut.get('status')}")
            return {"live": True, "spot_order": spot, "futures_order": fut, "symbol": symbol, "quantity": str(qty)}
        except Exception as exc:
            if spot_filled:
                try:
                    unwind = self.spot.new_market_order(symbol, "SELL", quantity=qty)
                except Exception as unwind_exc:
                    raise FuturesError(f"hedge leg failed and spot unwind failed: {exc}; {unwind_exc}") from exc
                raise FuturesError(f"hedge leg failed; spot was unwound: {exc}; unwind={unwind}") from exc
            raise
