"""Unified multi-family DEX adapter.

Composes the EVM adapter (``dex_evm``) and the non-EVM adapter
(``dex_nonevm``) behind one interface so the cross-exchange engine can scan
every configured chain with the same calls:

* ``chains()``                     — all enabled chain keys
* ``sources(chain_id)``            — venue names for a chain
* ``quote_unified(...)``           — a normalised quote for any chain/venue
* ``native_to_quote_rate(...)``    — gas-token pricing for cost accounting
* ``close()``                      — release RPC connections
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from decimal import Decimal

from .chains import env_chain_ids, get_spec
from .dex import DexQuote
from .dex_evm import EvmDexAdapter, RpcRateLimitError


@dataclass(frozen=True)
class ChainKey:
    """A chain selector that is an int for EVM chains and a str for non-EVM."""

    value: int | str

    @property
    def is_evm(self) -> bool:
        return isinstance(self.value, int)


class MultiChainDexAdapter:
    def __init__(self, evm_chain_ids: list[int] | None = None, non_evm_families: list[str] | None = None):
        self.evm: EvmDexAdapter | None = None
        self.nonevm = None
        errors: list[str] = []
        try:
            self.evm = EvmDexAdapter(chain_ids=evm_chain_ids)
        except RpcRateLimitError as exc:
            errors.append(f"evm: {exc}")
            logging.warning("EVM adapter unavailable: %s", exc)
        try:
            from .dex_nonevm import NonEvmDexAdapter

            families = non_evm_families if non_evm_families is not None else _env_nonevm_families()
            if families:
                self.nonevm = NonEvmDexAdapter(families)
        except Exception as exc:
            errors.append(f"nonevm: {exc}")
            logging.warning("non-EVM adapter unavailable: %s", exc)
        if self.evm is None and self.nonevm is None:
            raise RpcRateLimitError("no DEX adapter could be initialized: " + " | ".join(errors))

    # --- inventory ---------------------------------------------------------

    def chains(self) -> tuple[int | str, ...]:
        keys: list[int | str] = []
        if self.evm is not None:
            keys.extend(self.evm.chains())
        if self.nonevm is not None:
            keys.extend(self.nonevm.chains())
        return tuple(keys)

    def sources(self, chain: int | str) -> tuple[str, ...]:
        if isinstance(chain, int) and self.evm is not None:
            return self.evm.sources(chain)
        if isinstance(chain, str) and self.nonevm is not None:
            return self.nonevm.venues(chain)
        return ()

    def chain_label(self, chain: int | str) -> str:
        if isinstance(chain, int):
            spec = get_spec(chain)
            return spec.name if spec else str(chain)
        return str(chain)

    # --- quoting -----------------------------------------------------------

    def quote_single_source(self, *, chain_id=None, chain=None, sell_token: str, buy_token: str, sell_amount: int, taker: str = "", source: str, slippage_bps: int = 50, deadline: float | None = None, probe: bool = False):
        """Engine-compatible quote entrypoint.

        ``DexCrossExchangeEngine`` calls this with an int ``chain_id``; the
        non-EVM scanner calls it with a string ``chain``. Both are routed to the
        right adapter, and the non-EVM side returns ``(quote, None)`` because it
        has no EVM calldata.
        """
        if chain is not None and not isinstance(chain, int):
            return self.quote_unified(
                chain=chain, venue=source, sell_token=sell_token, buy_token=buy_token,
                sell_amount=sell_amount, taker=taker, slippage_bps=slippage_bps, probe=probe,
            )
        if self.evm is None:
            raise ValueError("EVM adapter is not available")
        return self.evm.quote_single_source(
            chain_id=chain_id, sell_token=sell_token, buy_token=buy_token,
            sell_amount=sell_amount, taker=taker, source=source,
            slippage_bps=slippage_bps, deadline=deadline, probe=probe,
        )

    def quote_unified(self, *, chain: int | str, venue: str, sell_token: str, buy_token: str, sell_amount: int, taker: str = "", slippage_bps: int = 50, probe: bool = True):
        """Return ``(DexQuote, execution_or_none)`` for any supported chain."""
        if isinstance(chain, int):
            if self.evm is None:
                raise ValueError("EVM adapter is not available")
            return self.evm.quote_single_source(
                chain_id=chain, sell_token=sell_token, buy_token=buy_token,
                sell_amount=sell_amount, taker=taker, source=venue,
                slippage_bps=slippage_bps, probe=probe,
            )
        if self.nonevm is None:
            raise ValueError("non-EVM adapter is not available")
        quote = self.nonevm.quote(
            chain=chain, venue=venue, sell_denom=sell_token, buy_denom=buy_token,
            sell_amount=sell_amount, slippage_bps=slippage_bps,
        )
        return quote, None

    def native_to_quote_rate(self, *, chain: int | str = None, chain_id: int | str = None, quote_token: str, sell_amount_native: int, taker: str = "") -> Decimal:
        target = chain if chain is not None else chain_id
        if isinstance(target, int) and self.evm is not None:
            return self.evm.native_to_quote_rate(
                chain_id=target, quote_token=quote_token,
                sell_amount_native=sell_amount_native, taker=taker,
            )
        # Non-EVM gas is not priced into the quote path yet; report an unknown
        # rate so the engine treats gas accounting as neutral for those chains.
        return Decimal("0")

    def rpc_status(self):
        return self.evm.rpc_status() if self.evm is not None else []

    def close(self) -> None:
        if self.evm is not None:
            self.evm.close()
        if self.nonevm is not None:
            self.nonevm.close()


def _env_nonevm_families() -> list[str]:
    raw = os.getenv("NONEVM_CHAINS", "").strip()
    return [x.strip().lower() for x in raw.split(",") if x.strip()]
