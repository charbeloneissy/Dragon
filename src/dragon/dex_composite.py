from __future__ import annotations

import os

from .dex_direct import DirectDexAdapter
from .dex_0x import ZeroXAdapter


class CompositeDexAdapter:
    """Combines direct DEX adapters with isolated 0x single-source liquidity."""

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
        if self.zerox is not None:
            try:
                external = []
                direct_names = {x.lower() for x in sources}
                for name in self.zerox.sources(chain_id):
                    # 0x sources are exposed as independent logical venues only
                    # when they are not already represented by Dragon's direct adapters.
                    if name.lower() in direct_names:
                        continue
                    external.append(f"0x:{name}")
                configured = [x.strip() for x in os.getenv("ZEROX_SOURCE_ALLOWLIST", "").split(",") if x.strip()]
                if configured:
                    allowed = set(configured)
                    external = [x for x in external if x.split(":", 1)[1] in allowed]
                external = external[: self.max_zerox_sources]
                sources.extend(external)
            except Exception as exc:
                import logging
                logging.warning("0x source discovery unavailable; continuing with direct DEXs: %s", exc)
        self._sources = tuple(dict.fromkeys(sources))
        return self._sources

    def clone_for_concurrent_quotes(self):
        return CompositeDexAdapter(self.direct.clone_for_concurrent_quotes())

    def quote_single_source(self, *, chain_id: int, sell_token: str, buy_token: str,
                            sell_amount: int, taker: str, source: str,
                            slippage_bps: int = 50):
        if source.startswith("0x:"):
            if self.zerox is None:
                raise RuntimeError("ZEROX_API_KEY is not configured")
            underlying = source.split(":", 1)[1]
            return self.zerox.quote_single_source(
                chain_id=chain_id,
                sell_token=sell_token,
                buy_token=buy_token,
                sell_amount=sell_amount,
                taker=taker,
                source=underlying,
                slippage_bps=slippage_bps,
            )
        return self.direct.quote_single_source(
            chain_id=chain_id,
            sell_token=sell_token,
            buy_token=buy_token,
            sell_amount=sell_amount,
            taker=taker,
            source=source,
            slippage_bps=slippage_bps,
        )

    def native_to_quote_rate(self, *, chain_id: int, quote_token: str,
                             sell_amount_native: int, taker: str):
        return self.direct.native_to_quote_rate(
            chain_id=chain_id,
            quote_token=quote_token,
            sell_amount_native=sell_amount_native,
            taker=taker,
        )

    def flash_loan_fee_bps(self):
        return self.direct.flash_loan_fee_bps()

    def close(self):
        try:
            self.direct.close()
        finally:
            if self.zerox is not None:
                self.zerox.close()
