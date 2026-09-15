import hashlib
import hmac
import os
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
        self.key = self._normalize_credential(api_key)
        self.secret = self._normalize_credential(api_secret)
        self.time_offset_ms = 0
        self._last_time_sync = 0.0
        self._time_sync_interval = float(os.getenv("BINANCE_TIME_SYNC_SECONDS", "30"))
        self.recv_window_ms = int(os.getenv("BINANCE_RECV_WINDOW_MS", "5000"))
        self.recv_window_ms = max(1000, min(60000, self.recv_window_ms))
        self._validate_credentials()
        self.http = httpx.Client(
            timeout=timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    @staticmethod
    def _normalize_credential(value: str) -> str:
        """Normalize Render/env-var pasted credentials without ever logging their values."""
        value = (value or "").strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1].strip()
        return value

    @staticmethod
    def _credential_encoding_error(name: str, value: str) -> BinanceError:
        bad = [(i, f"U+{ord(ch):04X}") for i, ch in enumerate(value) if ord(ch) > 127]
        positions = ", ".join(f"{i}:{code}" for i, code in bad[:8])
        suffix = "" if len(bad) <= 8 else f" (+{len(bad) - 8} more)"
        return BinanceError(
            f"{name} contains non-ASCII characters; length={len(value)}, "
            f"invalid_positions=[{positions}]. Replace it with the raw Binance credential."
        )

    def _validate_credentials(self):
        if self.key:
            try:
                self.key.encode("ascii")
            except UnicodeEncodeError as exc:
                raise self._credential_encoding_error("BINANCE_API_KEY", self.key) from exc
        if self.secret:
            try:
                self.secret.encode("ascii")
            except UnicodeEncodeError as exc:
                raise self._credential_encoding_error("BINANCE_API_SECRET", self.secret) from exc

        if os.getenv("DRAGON_CRED_DIAGNOSTIC", "").strip().lower() in {"1", "true", "yes", "on"}:
            print(
                "CREDENTIAL DIAGNOSTIC | "
                f"key_present={bool(self.key)} key_len={len(self.key)} "
                f"secret_present={bool(self.secret)} secret_len={len(self.secret)}",
                flush=True,
            )

    def close(self):
        self.http.close()

    @staticmethod
    def _retry_delay(response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After") if response is not None else None
        try:
            if retry_after:
                return min(15.0, max(0.5, float(retry_after)))
        except (ValueError, TypeError):
            pass
        return min(15.0, 1.0 * (2 ** attempt))

    def public(self, path: str, params=None):
        for attempt in range(5):
            try:
                r = self.http.get(self.base + path, params=params or {})
                if r.status_code in (418, 429) or 500 <= r.status_code < 600:
                    if attempt < 4:
                        time.sleep(self._retry_delay(r, attempt))
                        continue
                r.raise_for_status()
                return r.json()
            except httpx.HTTPStatusError as exc:
                raise BinanceError(
                    f"public request failed: {exc.response.text[:500]}",
                    status_code=exc.response.status_code,
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise BinanceError(f"public request failed: {exc}") from exc
        raise BinanceError("public request retry limit exceeded")

    def sync_time(self):
        local_before = int(time.time() * 1000)
        server = self.public("/api/v3/time")
        local_after = int(time.time() * 1000)
        midpoint = (local_before + local_after) // 2
        self.time_offset_ms = int(server["serverTime"]) - midpoint
        self._last_time_sync = time.monotonic()
        print(f"DRAGON TIME | Binance clock offset={self.time_offset_ms}ms", flush=True)
        return self.time_offset_ms

    def _ensure_time_sync(self):
        if time.monotonic() - self._last_time_sync >= self._time_sync_interval:
            self.sync_time()

    def ticker_24hr(self):
        return self.public("/api/v3/ticker/24hr")

    def book_ticker(self):
        """Return current best bid/ask for all Spot symbols in one public call."""
        return self.public("/api/v3/ticker/bookTicker")

    def signed(self, method: str, path: str, params=None):
        if not self.key or not self.secret:
            raise BinanceError("Binance credentials missing")

        method = method.upper()
        self._ensure_time_sync()
        p = {k: v for k, v in (params or {}).items() if v is not None}
        p["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
        p["recvWindow"] = p.get("recvWindow", self.recv_window_ms)
        query = urlencode(p, doseq=True)
        signature = hmac.new(
            self.secret.encode("ascii"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        wire = f"{query}&signature={signature}"
        headers = {
            "X-MBX-APIKEY": self.key,
            "Content-Type": "application/x-www-form-urlencoded",
        }

        try:
            if method == "GET":
                for attempt in range(4):
                    r = self.http.get(self.base + path + "?" + wire, headers=headers)
                    if r.status_code in (418, 429) and attempt < 3:
                        time.sleep(self._retry_delay(r, attempt))
                        continue
                    break
            elif method in {"POST", "PUT", "DELETE"}:
                r = self.http.request(method, self.base + path, content=wire, headers=headers)
            else:
                raise BinanceError(f"unsupported signed HTTP method: {method}")
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

            if code == -1021 and method == "GET":
                self.sync_time()
                p["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
                p["recvWindow"] = self.recv_window_ms
                query = urlencode(p, doseq=True)
                signature = hmac.new(
                    self.secret.encode("ascii"), query.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                try:
                    retry = self.http.get(
                        self.base + path + "?" + query + "&signature=" + signature,
                        headers=headers,
                    )
                    if retry.status_code < 400:
                        return retry.json()
                    try:
                        payload = retry.json()
                        code = payload.get("code")
                        message = payload.get("msg", retry.text[:500])
                    except ValueError:
                        message = retry.text[:500]
                    raise BinanceError(message, status_code=retry.status_code, code=code, response=retry.text)
                except httpx.HTTPError as exc:
                    raise BinanceError(f"signed retry failed: {exc}") from exc

            raise BinanceError(message, status_code=r.status_code, code=code, response=r.text)

        try:
            return r.json()
        except ValueError as exc:
            raise BinanceError(f"Binance returned non-JSON response: {r.text[:500]}") from exc

    def account(self):
        return self.signed("GET", "/api/v3/account")

    def api_restrictions(self):
        """Return Binance API-key permissions for diagnostics and startup gating."""
        return self.signed("GET", "/sapi/v1/account/apiRestrictions")

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
