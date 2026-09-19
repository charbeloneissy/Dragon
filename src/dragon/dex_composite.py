from __future__ import annotations

import logging
import os

from .dex_direct import DirectDexAdapter
from .dex_0x import ZeroXAdapter


class CompositeDexAdapter:
    """Combines direct DEX adapters with optional aggregated 0x liquidity."""

    DIRECT_SOURCES = ("Uniswap_V3", "Aerodrome")

    def __init__(self, direct: DirectDexAdapter):
        self.direct = direct
        self.zerox = ZeroXAdapter() if os.getenv("ZEROX_API_KEY", "").strip() else None
        self.max_zerox_sources = max(0, min(12, int(os.getenv("DEX_MAX_0X_SOURCES", "6"))))
        self._sources = None

    def sources(self, chain_id: int) -> tuple[str, ...]:
        if int(chain_id) != 8453:
            return ()
        if self._sources is not None:
            return self._sources

        sources = list(self.direct.sources(chain_id))
        # Require a separate opt-in because a stale API key or legacy environment
        # value must not silently re-enable an unreliable external venue.
        enable_0x = (
            self.zerox is not None
            and os.getenv("DEX_0X_OPT_IN", "false").strip().lower()
            in {"1", "true", "yes", "on"}
            and os.getenv("DEX_0X_AGGREGATED", "true").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        if enable_0x:
            sources.append("0x:AGGREGATED")
            logging.info("0x aggregated venue enabled on chain=%s", chain_id)

        self._sources = tuple(dict.fromkeys(sources))
        return self._sources

    def clone_for_concurrent_quotes(self):
        return CompositeDexAdapter(self.direct.clone_for_concurrent_quotes())

    def quote_single_source(
        self, *, chain_id: int, sell_token: str, buy_token: str,
        sell_amount: int, taker: str, source: str, slippage_bps: int = 50,
        deadline: float | None = None, probe: bool = False
    ):
        if source.startswith("0x:"):
            if self.zerox is None:
                raise RuntimeError("ZEROX_API_KEY is not configured")
            underlying = source.split(":", 1)[1]
            if underlying == "AGGREGATED":
                return self.zerox.quote(
                    chain_id=chain_id, sell_token=sell_token, buy_token=buy_token,
                    sell_amount=sell_amount, taker=taker, slippage_bps=slippage_bps,
                )
            return self.zerox.quote_single_source(
                chain_id=chain_id, sell_token=sell_token, buy_token=buy_token,
                sell_amount=sell_amount, taker=taker, source=underlying,
                slippage_bps=slippage_bps,
            )
        return self.direct.quote_single_source(
            chain_id=chain_id, sell_token=sell_token, buy_token=buy_token,
            sell_amount=sell_amount, taker=taker, source=source,
            slippage_bps=slippage_bps,
            deadline=deadline, probe=probe,
        )

    def native_to_quote_rate(self, *, chain_id: int, quote_token: str,
                             sell_amount_native: int, taker: str):
        return self.direct.native_to_quote_rate(
            chain_id=chain_id, quote_token=quote_token,
            sell_amount_native=sell_amount_native, taker=taker,
        )

    def flash_loan_fee_bps(self):
        return self.direct.flash_loan_fee_bps()

    def close(self):
        try:
            self.direct.close()
        finally:
            if self.zerox is not None:
                self.zerox.close()
