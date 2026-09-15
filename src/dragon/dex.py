from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class DexQuote:
    chain: str
    venue: str
    sell_token: str
    buy_token: str
    sell_amount: Decimal
    buy_amount: Decimal
    gas_native: Decimal
    gas_quote: Decimal
    fee_bps: Decimal
    slippage_bps: Decimal
    latency_ms: Decimal = Decimal("0")

    @property
    def gross_bps(self) -> Decimal:
        if self.sell_amount <= 0:
            return Decimal("-Infinity")
        return (self.buy_amount / self.sell_amount - Decimal("1")) * Decimal("10000")


class DexQuoteProvider:
    """Provider-neutral DEX quote interface.

    The adapter is intentionally quote-only. A DEX execution adapter must be
    explicitly configured before Dragon can send an on-chain transaction.
    """

    def quote(self, **kwargs) -> DexQuote:
        raise NotImplementedError


class HttpDexQuoteProvider(DexQuoteProvider):
    """Consume a configured JSON quote endpoint and preserve gas/slippage data.

    Expected response fields:
      chain, venue, sellToken, buyToken, sellAmount, buyAmount,
      gasNative, gasQuote, feeBps, slippageBps, latencyMs(optional)

    No endpoint means DEX scanning is disabled and fails closed.
    """

    def __init__(self, url: str | None = None, timeout: float = 2.0):
        self.url = (url or os.getenv("DEX_QUOTE_URL", "")).strip()
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def quote(self, **kwargs) -> DexQuote:
        if not self.enabled:
            raise RuntimeError("DEX quote provider is not configured")
        query = urlencode({k: str(v) for k, v in kwargs.items()})
        url = self.url + ("&" if "?" in self.url else "?") + query
        started = __import__("time").perf_counter()
        req = Request(url, headers={"Accept": "application/json", "User-Agent": "Dragon-Arbitrage/1.0"})
        with urlopen(req, timeout=self.timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        latency_ms = Decimal(str(payload.get("latencyMs", (__import__("time").perf_counter() - started) * 1000)))
        return DexQuote(
            chain=str(payload["chain"]),
            venue=str(payload["venue"]),
            sell_token=str(payload["sellToken"]),
            buy_token=str(payload["buyToken"]),
            sell_amount=Decimal(str(payload["sellAmount"])),
            buy_amount=Decimal(str(payload["buyAmount"])),
            gas_native=Decimal(str(payload.get("gasNative", "0"))),
            gas_quote=Decimal(str(payload.get("gasQuote", "0"))),
            fee_bps=Decimal(str(payload.get("feeBps", "0"))),
            slippage_bps=Decimal(str(payload.get("slippageBps", "0"))),
            latency_ms=latency_ms,
        )
