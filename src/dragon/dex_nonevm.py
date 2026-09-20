"""Non-EVM DEX adapters for Dragon.

Three families are supported, each with its own quote source:

* ``tron``   — SunSwap V2 (TVM), quoted through TronGrid ``triggerconstantcontract``.
* ``cosmos`` — Osmosis (via the SQS router) and Astroport (via a Cosmos LCD
               ``simulate_swap_operations`` smart query).
* ``aptos``  — Liquidswap (Move) ``router::get_amount_out`` view calls.
* ``solana`` — Jupiter quote API (read-only route aggregation).

Every adapter exposes the same ``quote`` / ``venues`` / ``close`` surface the
EVM adapter uses, so the cross-exchange engine can treat all chains uniformly.
Quotes are read-only: none of these adapters sign or broadcast a transaction.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal

from .dex import DexQuote


@dataclass(frozen=True)
class NonEvmVenue:
    chain: str
    name: str
    kind: str
    endpoint: str
    note: str = ""


class NonEvmDexAdapter:
    """Read-only multi-family DEX quoter for Tron, Cosmos and Aptos."""

    def __init__(self, families: list[str] | None = None):
        wanted = families if families is not None else _env_families()
        self._timeout = float(os.getenv("NONEVM_HTTP_TIMEOUT_SECONDS", "8"))
        self._venues: dict[str, dict[str, NonEvmVenue]] = {}
        if "tron" in wanted:
            self._venues["tron"] = self._tron_venues()
        if "cosmos" in wanted:
            self._venues["cosmos"] = self._cosmos_venues()
        if "aptos" in wanted:
            self._venues["aptos"] = self._aptos_venues()
        if "solana" in wanted:
            self._venues["solana"] = self._solana_venues()
        if not self._venues:
            raise RuntimeError("no non-EVM family enabled (set NONEVM_CHAINS)")

    # --- registries --------------------------------------------------------

    @staticmethod
    def _tron_venues() -> dict[str, NonEvmVenue]:
        return {
            "SunSwap_V2": NonEvmVenue(
                chain="tron", name="SunSwap_V2", kind="sunswap_v2",
                endpoint="https://api.trongrid.io",
                note="SunSwapV2Router02 TKzxdSv2FZKQrEqkKVgp5DcwEXBEKMg2Ax",
            ),
        }

    @staticmethod
    def _cosmos_venues() -> dict[str, NonEvmVenue]:
        sqs = os.getenv("OSMOSIS_SQS_URL", "https://sqs.osmosis.zone").strip()
        venues = {
            "Osmosis": NonEvmVenue(chain="cosmos", name="Osmosis", kind="osmosis_sqs", endpoint=sqs),
        }
        # Astroport needs a chain-specific router address and a LCD host that does
        # not block server clients. Both are operator-configurable; without them
        # the venue stays off rather than failing every scan.
        neutron_router = os.getenv("ASTROPORT_NEUTRON_ROUTER", "").strip()
        if neutron_router:
            venues["Astroport_Neutron"] = NonEvmVenue(
                chain="cosmos", name="Astroport_Neutron", kind="astroport",
                endpoint=os.getenv("NEUTRON_LCD_URL", "").strip(), note=neutron_router,
            )
        sei_router = os.getenv("ASTROPORT_SEI_ROUTER", "").strip()
        if sei_router:
            venues["Astroport_Sei"] = NonEvmVenue(
                chain="cosmos", name="Astroport_Sei", kind="astroport",
                endpoint=os.getenv("SEI_LCD_URL", "https://rest.sei-apis.com").strip(), note=sei_router,
            )
        return venues

    @staticmethod
    def _aptos_venues() -> dict[str, NonEvmVenue]:
        return {
            "Liquidswap": NonEvmVenue(
                chain="aptos", name="Liquidswap", kind="liquidswap",
                endpoint=os.getenv("APTOS_NODE_URL", "https://fullnode.mainnet.aptoslabs.com").strip(),
                note="0x190d44266241744264b964a37b8f09863167a12d3e70cda39376cfb4e3561e12",
            ),
        }

    @staticmethod
    def _solana_venues() -> dict[str, NonEvmVenue]:
        return {
            "Jupiter": NonEvmVenue(
                chain="solana", name="Jupiter", kind="jupiter",
                endpoint=os.getenv("JUPITER_QUOTE_URL", "https://api.jup.ag/swap/v1/quote").strip(),
                note="Bearer auth via JUPITER_API_KEY",
            ),
        }

    # --- interface ---------------------------------------------------------

    def chains(self) -> tuple[str, ...]:
        return tuple(self._venues.keys())

    def venues(self, chain: str) -> tuple[str, ...]:
        return tuple(self._venues.get(chain, {}).keys())

    def close(self) -> None:
        return None

    def quote(self, *, chain: str, venue: str, sell_denom: str, buy_denom: str, sell_amount: int, slippage_bps: int = 50) -> DexQuote:
        entry = self._venues.get(chain, {}).get(venue)
        if entry is None:
            raise ValueError(f"unsupported non-EVM venue {venue} on {chain}")
        if int(sell_amount) <= 0:
            raise ValueError("sell_amount must be positive")
        if entry.kind == "sunswap_v2":
            out = self._sunswap_quote(entry, sell_denom, buy_denom, int(sell_amount))
        elif entry.kind == "osmosis_sqs":
            out = self._osmosis_quote(entry, sell_denom, buy_denom, int(sell_amount))
        elif entry.kind == "astroport":
            out = self._astroport_quote(entry, sell_denom, buy_denom, int(sell_amount))
        elif entry.kind == "liquidswap":
            out = self._liquidswap_quote(entry, sell_denom, buy_denom, int(sell_amount))
        elif entry.kind == "jupiter":
            out = self._jupiter_quote(entry, sell_denom, buy_denom, int(sell_amount), slippage_bps)
        else:
            raise ValueError(f"unsupported non-EVM kind {entry.kind}")
        if out <= 0:
            raise RuntimeError(f"{venue} returned zero output")
        return DexQuote(
            chain=chain, venue=venue, sell_token=sell_denom, buy_token=buy_denom,
            sell_amount=Decimal(sell_amount), buy_amount=Decimal(out),
            gas_native=Decimal("0"), gas_quote=Decimal("0"), fee_bps=Decimal("0"),
            slippage_bps=Decimal(slippage_bps),
        )

    # --- HTTP helpers ------------------------------------------------------

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> dict:
        request_headers = {"Accept": "application/json", "User-Agent": "Dragon-Arbitrage/2.1"}
        if headers:
            request_headers.update(headers)
        req = urllib.request.Request(url, headers=request_headers)
        with urllib.request.urlopen(req, timeout=self._timeout) as response:
            return json.loads(response.read().decode())

    def _post_json(self, url: str, body: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "Dragon-Arbitrage/2.1"})
        with urllib.request.urlopen(req, timeout=self._timeout) as response:
            return json.loads(response.read().decode())

    # --- Tron / SunSwap V2 -------------------------------------------------

    TRON_ROUTER = "TKzxdSv2FZKQrEqkKVgp5DcwEXBEKMg2Ax"
    TRON_FACTORY = "TKWJdrQkqHisa1X8HUdHEfREvTzw4pMAaY"

    def _sunswap_quote(self, venue: NonEvmVenue, sell: str, buy: str, amount: int) -> int:
        parameter = _tron_encode_get_amounts_out(amount, sell, buy)
        body = {
            "owner_address": sell,
            "contract_address": self.TRON_ROUTER,
            "function_selector": "getAmountsOut(uint256,address[])",
            "parameter": parameter,
            "visible": True,
        }
        payload = self._post_json(f"{venue.endpoint}/wallet/triggerconstantcontract", body)
        results = payload.get("constant_result") or []
        if not results:
            message = str(payload.get("result") or payload)[:200]
            raise RuntimeError(f"SunSwap quote failed: {message}")
        raw = results[0]
        amounts = _decode_uint_array(raw)
        if not amounts:
            raise RuntimeError("SunSwap returned no amounts")
        return amounts[-1]

    # --- Osmosis -----------------------------------------------------------

    def _osmosis_quote(self, venue: NonEvmVenue, sell: str, buy: str, amount: int) -> int:
        query = urllib.parse.urlencode({"tokenIn": f"{amount}{sell}", "tokenOutDenom": buy})
        data = self._get_json(f"{venue.endpoint}/router/quote?{query}")
        return int(data["amount_out"])

    # --- Astroport ---------------------------------------------------------

    ASTROPORT_ROUTERS = {
        "Astroport_Neutron": "neutron1eeyntmsq448c68ez06jsy6h2mtjke5tpuplnwtjfwcdznqmw72kswnlmm0",
        "Astroport_Sei": "sei1n389228apfytkxgvjkwl3acakgl8evpx7z5nghwvluhwsjwq37gqjatsxy",
    }

    def _astroport_quote(self, venue: NonEvmVenue, sell: str, buy: str, amount: int) -> int:
        router = venue.note
        if not router or not venue.endpoint:
            raise RuntimeError(f"Astroport venue {venue.name} is not fully configured (router/LCD)")
        operation = {"native_swap": {"offer_denom": sell, "ask_denom": buy}}
        query = {"simulate_swap_operations": {"offer_amount": str(amount), "operations": [operation]}}
        payload = _cosmos_smart_query(venue.endpoint, router, query)
        return int(payload["amount"])

    # --- Aptos / Liquidswap ------------------------------------------------

    LIQUIDSWAP = "0x190d44266241744264b964a37b8f09863167a12d3e70cda39376cfb4e3561e12"
    APTOS_CURVES = {
        "stable": f"{LIQUIDSWAP}::curves::Stable",
        "uncorrelated": f"{LIQUIDSWAP}::curves::Uncorrelated",
    }

    def _liquidswap_quote(self, venue: NonEvmVenue, sell: str, buy: str, amount: int) -> int:
        """Liquidswap address pairs are ``TYPE::CURVE``, e.g. ``0x1::aptos_coin::AptosCoin::stable``."""
        sell_type, _, curve_key = sell.rpartition("::")
        buy_type, _, _ = buy.rpartition("::")
        curve = self.APTOS_CURVES.get(curve_key)
        if not curve or not sell_type or not buy_type:
            raise ValueError("Aptos quotes need sell/buy as TYPE::curve where curve is stable|uncorrelated")
        body = {
            "function": f"{self.LIQUIDSWAP}::router::get_amount_out",
            "type_arguments": [sell_type, buy_type, curve],
            "arguments": [str(amount)],
        }
        payload = self._post_json(f"{venue.endpoint}/v1/view", body)
        if isinstance(payload, list):
            payload = payload[0] if payload else 0
        return int(payload)

    def _jupiter_quote(self, venue: NonEvmVenue, sell: str, buy: str, amount: int, slippage_bps: int) -> int:
        api_key = os.getenv("JUPITER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("Jupiter is enabled but JUPITER_API_KEY is not configured")
        query = urllib.parse.urlencode({
            "inputMint": sell,
            "outputMint": buy,
            "amount": str(amount),
            "slippageBps": str(max(0, int(slippage_bps))),
        })
        payload = self._get_json(
            f"{venue.endpoint}?{query}",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        if payload.get("error") or payload.get("code", 0) >= 400:
            raise RuntimeError(f"Jupiter quote failed: {payload.get('error') or payload.get('message') or payload}")
        out = payload.get("outAmount")
        if out is None:
            raise RuntimeError("Jupiter returned no outAmount")
        return int(out)


def _env_families() -> list[str]:
    raw = os.getenv("NONEVM_CHAINS", "").strip()
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _tron_encode_get_amounts_out(amount: int, token_in: str, token_out: str) -> str:
    """ABI-encode getAmountsOut(uint256,address[]) for a two-hop path."""
    path = _tron_abi_address_array([token_in, token_out])
    head = f"{amount:064x}"
    offset = f"{64:064x}"
    return head + offset + path


def _tron_abi_address_array(addresses: list[str]) -> str:
    length = f"{len(addresses):064x}"
    words = "".join(_tron_abi_address(a) for a in addresses)
    return length + words


def _tron_base58_to_hex41(address: str) -> str:
    """Decode a Tron base58check address to its 21-byte 0x41-prefixed hex form.

    A Tron address is ``version(1) + payload(20) + checksum(4)``. The first
    payload byte is already ``0x41`` only for mainnet addresses whose payload
    happens to start with 0x41; the version byte is separate, so the returned
    hex is ``0x41`` followed by the 20 payload bytes.
    """
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    num = 0
    for char in address:
        num = num * 58 + alphabet.index(char)
    raw = num.to_bytes(25, "big")
    if len(raw) != 25:
        raise ValueError(f"invalid Tron address length for {address}")
    payload = raw[1:21]
    return "41" + payload.hex()


def _tron_abi_address(address: str) -> str:
    hex41 = _tron_base58_to_hex41(address)
    # Drop the 0x41 version byte; the remaining 20 payload bytes are the ABI word.
    return "0" * 24 + hex41[2:]


def _decode_uint_array(raw_hex: str) -> list[int]:
    data = bytes.fromhex(raw_hex)
    if len(data) < 64:
        return []
    length = int.from_bytes(data[32:64], "big")
    values = []
    for i in range(length):
        start = 64 + i * 32
        chunk = data[start:start + 32]
        if len(chunk) < 32:
            break
        values.append(int.from_bytes(chunk, "big"))
    return values


def _cosmos_smart_query(lcd: str, contract: str, query: dict) -> dict:
    """Run a CosmWasm smart query and decode the base64 payload."""
    import base64

    encoded = base64.b64encode(json.dumps(query).encode()).decode()
    url = f"{lcd.rstrip('/')}/cosmwasm/wasm/v1/contract/{contract}/smart/{encoded}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Dragon-Arbitrage/2.1"})
    with urllib.request.urlopen(req, timeout=10) as response:
        payload = json.loads(response.read().decode())
    data = payload.get("data")
    if isinstance(data, str):
        return json.loads(base64.b64decode(data).decode())
    return data or payload
