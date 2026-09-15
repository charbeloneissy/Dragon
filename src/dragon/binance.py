import hashlib
import hmac
import time
from urllib.parse import urlencode

import httpx


class BinanceError(RuntimeError):
    pass


class BinanceClient:
    def __init__(self, api_base: str, api_key: str = "", api_secret: str = ""):
        self.base = api_base.rstrip("/")
        self.key = api_key.strip()
        self.secret = api_secret.strip()
        self.http = httpx.Client(timeout=5.0)

    def public(self, path: str, params=None):
        r = self.http.get(self.base + path, params=params or {})
        r.raise_for_status()
        return r.json()

    def signed(self, method: str, path: str, params=None):
        if not self.key or not self.secret:
            raise BinanceError("Binance credentials missing")
        p = dict(params or {})
        p["timestamp"] = int(time.time() * 1000)
        p.setdefault("recvWindow", 5000)
        query = urlencode(p)
        sig = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        headers = {"X-MBX-APIKEY": self.key}
        r = self.http.request(method, self.base + path + "?" + query + "&signature=" + sig, headers=headers)
        if r.status_code >= 400:
            raise BinanceError(r.text)
        return r.json()

    def account(self):
        return self.signed("GET", "/api/v3/account")

    def exchange_info(self):
        return self.public("/api/v3/exchangeInfo")
