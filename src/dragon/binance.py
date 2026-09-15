import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx


class BinanceError(RuntimeError):
    def __init__(self, message: str, *, status_code=None, code=None, response=None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.response = response


class BinanceClient:
    def __init__(self, api_base: str, api_key: str = "", api_secret: str = "", timeout: float = 5.0):
        self.base = api_base.rstrip("/")
        self.key = api_key.strip()
        self.secret = api_secret.strip()
        self.time_offset_ms = 0
        self.http = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    def close(self):
        self.http.close()

    def public(self, path: str, params=None):
        try:
            r = self.http.get(self.base + path, params=params or {})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            raise BinanceError(f"public request failed: {exc.response.text[:500]}", status_code=exc.response.status_code) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BinanceError(f"public request failed: {exc}") from exc

    def sync_time(self):
        local_before = int(time.time() * 1000)
        server = self.public("/api/v3/time")
        local_after = int(time.time() * 1000)
        midpoint = (local_before + local_after) // 2
        self.time_offset_ms = int(server["serverTime"]) - midpoint
        return self.time_offset_ms

    def signed(self, method: str, path: str, params=None):
        if not self.key or not self.secret:
            raise BinanceError("Binance credentials missing")
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        p.setdefault("recvWindow", 5000)
        query = urlencode(p, doseq=True)
        sig = hmac.new(self.secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": self.key}
        try:
            r = self.http.request(method, self.base + path, params={**p, "signature": sig}, headers=headers)
        except httpx.HTTPError as exc:
            raise BinanceError(f"signed request failed before response: {exc}") from exc
        if r.status_code >= 400:
            code = None
            try:
                payload = r.json()
                code = payload.get("code")
                message = payload.get("msg", r.text[:500])
            except ValueError:
                message = r.text[:500]
            raise BinanceError(message, status_code=r.status_code, code=code, response=r.text)
        try:
            return r.json()
        except ValueError as exc:
            raise BinanceError(f"Binance returned non-JSON response: {r.text[:500]}") from exc

    def account(self):
        return self.signed("GET", "/api/v3/account")

    def exchange_info(self):
        return self.public("/api/v3/exchangeInfo")

    def order(self, symbol: str, order_id: int):
        return self.signed("GET", "/api/v3/order", {"symbol": symbol, "orderId": order_id})

    def new_market_order(self, symbol: str, side: str, *, quantity=None, quote_order_qty=None):
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "newOrderRespType": "FULL",
        }
        if quantity is not None:
            params["quantity"] = format(quantity, "f")
        elif quote_order_qty is not None:
            params["quoteOrderQty"] = format(quote_order_qty, "f")
        else:
            raise BinanceError("market order requires quantity or quote_order_qty")
        return self.signed("POST", "/api/v3/order", params)

    def cancel_order(self, symbol: str, order_id: int):
        return self.signed("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id})
