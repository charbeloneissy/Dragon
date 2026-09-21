from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


DEFAULT_STABLECOINS = (
    "USDC",
    "USDT",
    "DAI",
    "GHO",
    "USDe",
    "USDS",
    "PYUSD",
    "FRAX",
    "crvUSD",
)

_READ_TOOLS = {
    "get_chains",
    "get_markets",
    "get_reserve_details",
    "get_emode_categories",
    "get_apy_history",
    "get_protocol_history",
    "get_hubs",
    "get_hub_assets",
    "get_user_positions",
    "get_position_items",
    "get_user_summary",
    "get_user_summary_history",
    "get_user_activity",
    "get_transaction_processed",
    "get_user_rewards",
    "get_swappable_tokens",
    "get_swap_quote",
    "get_pending_orders",
    "get_order_status",
    "get_sgho_vault",
    "get_sgho_preview",
    "search_governance_proposals",
    "get_governance_proposal",
    "get_proposal_votes",
    "get_user_vote",
    "get_proposal_payloads",
    "get_started",
    "get_aave_guide",
}

# These tools only prepare/simulate. They never sign or relay a transaction.
_UNSIGNED_TOOLS = {
    "preview_action",
    "prepare_action",
    "prepare_set_collateral",
    "prepare_set_emode",
    "prepare_liquidation",
    "prepare_claim_rewards",
    "prepare_order",
    "prepare_cancel_order",
    "prepare_sgho_action",
    "prepare_stkgho_migrate",
    "vaultSetFee",
    "vaultWithdrawFees",
    "vaultTransferOwnership",
}

# Explicitly rejected because they relay a signature or cancellation to a live order.
_BLOCKED_TOOLS = {
    "submit_signed_order",
    "cancel_order",
}


class AaveMCPError(RuntimeError):
    pass


@dataclass(frozen=True)
class AaveMarketSnapshot:
    version: str
    chain_id: int | None
    chain: str
    symbol: str
    reserve_id: str | None
    market_address: str | None
    underlying_token_address: str | None
    underlying_token_name: str | None
    supply_apy_pct: Decimal | None
    available_liquidity: Decimal | None
    utilization_pct: Decimal | None
    supply_cap: Decimal | None
    borrow_cap: Decimal | None
    frozen: bool
    paused: bool
    can_supply: bool | None
    can_use_as_collateral: bool | None
    suppliable: Decimal | None
    rewards: tuple[dict[str, Any], ...]
    raw: dict[str, Any]

    @property
    def incentive_apy_pct(self) -> Decimal:
        total = Decimal("0")
        for reward in self.rewards:
            value = reward.get("extraApy")
            if value is None:
                continue
            parsed = _pct(value)
            if parsed is not None:
                total += parsed
        return total

    @property
    def displayed_apy_pct(self) -> Decimal | None:
        if self.supply_apy_pct is None:
            return None
        return self.supply_apy_pct + self.incentive_apy_pct

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "chain_id": self.chain_id,
            "chain": self.chain,
            "symbol": self.symbol,
            "reserve_id": self.reserve_id,
            "market_address": self.market_address,
            "underlying_token_address": self.underlying_token_address,
            "underlying_token_name": self.underlying_token_name,
            "supply_apy_pct": _string(self.supply_apy_pct),
            "incentive_apy_pct": _string(self.incentive_apy_pct),
            "displayed_apy_pct": _string(self.displayed_apy_pct),
            "available_liquidity": _string(self.available_liquidity),
            "utilization_pct": _string(self.utilization_pct),
            "supply_cap": _string(self.supply_cap),
            "borrow_cap": _string(self.borrow_cap),
            "frozen": self.frozen,
            "paused": self.paused,
            "can_supply": self.can_supply,
            "can_use_as_collateral": self.can_use_as_collateral,
            "suppliable": _string(self.suppliable),
            "rewards": list(self.rewards),
        }


def parse_human_amount(value: str) -> Decimal:
    """Parse Aave main-unit amounts: positive plain decimal, no exponent/hex."""
    text = str(value).strip()
    if not text or text.startswith(("+", "-")) or "e" in text.lower() or text.lower().startswith("0x"):
        raise ValueError("Aave amounts must be positive plain decimal strings")
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("Aave amounts must be positive plain decimal strings") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Aave amount must be positive and finite")
    return amount


def canonical_human_amount(value: str) -> str:
    amount = parse_human_amount(value)
    text = format(amount, "f").rstrip("0").rstrip(".")
    return text or "0"


def _validate_chain_coverage(result: Any, requested_chains: set[int] | None = None) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"chainsCovered": [], "chainsNotCovered": [], "chainsNotServed": [], "needs_retry": False}
    def normalize(values: Any) -> list[Any]:
        return list(values) if isinstance(values, (list, tuple)) else []
    covered = normalize(result.get("chainsCovered"))
    not_covered = normalize(result.get("chainsNotCovered"))
    not_served = normalize(result.get("chainsNotServed"))
    requested = requested_chains or set()
    if requested:
        missing_requested = [
            chain for chain in not_covered
            if str(chain) in {str(x) for x in requested}
        ]
    else:
        missing_requested = not_covered
    return {
        "chainsCovered": covered,
        "chainsNotCovered": not_covered,
        "chainsNotServed": not_served,
        "needs_retry": bool(missing_requested),
        "retry_chains": missing_requested,
    }


def _string(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _pct(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("value", "pct", "percentage", "amount"):
            if key in value:
                return _pct(value[key])
        return None
    try:
        return Decimal(str(value).replace("%", "").strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, dict):
        for key in ("value", "amount", "usd", "raw"):
            if key in value:
                return _decimal(value[key])
        return None
    try:
        value = Decimal(str(value).replace(",", "").strip())
        return value if value.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _deep_get(item: Any, *names: str) -> Any:
    if not isinstance(item, dict):
        return None
    lowered = {str(k).lower(): v for k, v in item.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    for value in item.values():
        if isinstance(value, dict):
            found = _deep_get(value, *names)
            if found is not None:
                return found
    return None


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk_dicts(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk_dicts(nested)


def _unwrap_result(value: Any) -> Any:
    """Normalize an MCP response without discarding safety metadata.

    Aave MCP returns an envelope containing data, next_actions and warnings.
    Older Dragon code unwrapped data recursively and lost warnings. We now
    preserve those fields while flattening the data payload for callers that
    already expect a dictionary/list.
    """
    if not isinstance(value, dict):
        return value

    structured = value.get("structuredContent")
    if isinstance(structured, dict):
        normalized = _unwrap_result(structured)
        if isinstance(normalized, dict):
            data = normalized.get("data")
            envelope = dict(data) if isinstance(data, dict) else {"data": data}
            for key in ("warnings", "next_actions", "chainsCovered", "chainsNotCovered", "chainsNotServed"):
                if key in normalized:
                    envelope[key] = normalized[key]
            envelope["_aave_envelope"] = normalized
            return envelope
        return normalized

    if isinstance(value.get("content"), list):
        texts = [
            x.get("text")
            for x in value["content"]
            if isinstance(x, dict) and x.get("text")
        ]
        for text in texts:
            try:
                parsed = json.loads(text)
                normalized = _unwrap_result(parsed)
                if isinstance(normalized, dict):
                    # Preserve top-level JSON-RPC/MCP metadata when present.
                    for key in ("warnings", "next_actions", "chainsCovered", "chainsNotCovered", "chainsNotServed"):
                        if key in value and key not in normalized:
                            normalized[key] = value[key]
                return normalized
            except Exception:
                continue

    if "data" in value and isinstance(value.get("data"), (dict, list)):
        data = value["data"]
        envelope = dict(data) if isinstance(data, dict) else {"data": data}
        for key in ("warnings", "next_actions", "chainsCovered", "chainsNotCovered", "chainsNotServed"):
            if key in value:
                envelope[key] = value[key]
        envelope["_aave_envelope"] = value
        return envelope

    return value


class AaveMCPClient:
    """Safe client for Aave's official MCP server.

    Dragon may read live protocol data, simulate actions, and prepare unsigned
    transactions. It never signs, relays signed orders, or stores private keys.
    The live tools/list inventory is authoritative when server capabilities change.
    """

    def __init__(self, url: str | None = None):
        self.url = (url or os.getenv("AAVE_MCP_URL", "https://mcp.aave.com")).rstrip("/")
        self.timeout = max(2.0, float(os.getenv("AAVE_MCP_TIMEOUT_SECONDS", "8")))
        self.cache_seconds = max(5.0, float(os.getenv("AAVE_MCP_CACHE_SECONDS", "60")))
        self.max_attempts = max(1, min(4, int(os.getenv("AAVE_MCP_MAX_ATTEMPTS", "3"))))
        self._request_id = 0
        self._lock = asyncio.Lock()
        self._cache: dict[str, tuple[float, Any]] = {}
        self._tool_inventory: dict[str, dict[str, Any]] | None = None
        self._tool_inventory_at: float = 0.0

    async def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        if tool in _BLOCKED_TOOLS:
            raise AaveMCPError(f"tool {tool!r} is blocked because it relays signed state-changing requests")
        if tool not in _READ_TOOLS and tool not in _UNSIGNED_TOOLS:
            raise AaveMCPError(f"tool {tool!r} is not allowed in Dragon's Aave adapter")
        if tool in _UNSIGNED_TOOLS:
            logging.info("Aave unsigned/simulation call tool=%s; no signing or broadcast is performed", tool)

        cache_key = json.dumps([tool, arguments], sort_keys=True, separators=(",", ":"))
        now = time.monotonic()
        cacheable = tool in _READ_TOOLS
        cached = self._cache.get(cache_key) if cacheable else None
        if cached and now - cached[0] < self.cache_seconds:
            return cached[1]

        async with self._lock:
            self._request_id += 1
            request_id = self._request_id

        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }

        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                ) as client:
                    response = await client.post(self.url + "/", json=payload)

                if response.status_code == 429:
                    retry_after = max(0.25, min(5.0, float(response.headers.get("retry-after", "1"))))
                    await asyncio.sleep(retry_after)
                    continue

                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    raise AaveMCPError(str(body["error"]))

                result = _unwrap_result(body.get("result", body))
                if cacheable:
                    self._cache[cache_key] = (time.monotonic(), result)
                return result
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(2.0, 0.25 * (attempt + 1)))

        raise AaveMCPError(f"Aave MCP {tool} failed: {last_error}") from last_error

    async def list_tools(self, *, refresh: bool = False) -> Any:
        """Return the server's live tools/list inventory.

        Aave documents tools/list as authoritative when its schema differs
        from the static documentation.
        """
        now = time.monotonic()
        if (
            not refresh
            and self._tool_inventory is not None
            and now - self._tool_inventory_at < self.cache_seconds
        ):
            return self._tool_inventory

        result = await self._rpc_request("tools/list", {})
        inventory: dict[str, dict[str, Any]] = {}
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            for item in tools:
                if isinstance(item, dict) and item.get("name"):
                    inventory[str(item["name"])] = item
        self._tool_inventory = inventory
        self._tool_inventory_at = time.monotonic()
        return inventory

    async def _rpc_request(self, method: str, params: dict[str, Any]) -> Any:
        async with self._lock:
            self._request_id += 1
            request_id = self._request_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout,
                    headers={
                        "content-type": "application/json",
                        "accept": "application/json, text/event-stream",
                    },
                ) as client:
                    response = await client.post(self.url + "/", json=payload)
                if response.status_code == 429:
                    retry_after = max(0.25, min(5.0, float(response.headers.get("retry-after", "1"))))
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                body = response.json()
                if "error" in body:
                    raise AaveMCPError(str(body["error"]))
                return _unwrap_result(body.get("result", body))
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    await asyncio.sleep(min(2.0, 0.25 * (attempt + 1)))
        raise AaveMCPError(f"Aave MCP {method} failed: {last_error}") from last_error

    async def ensure_tool(self, tool: str, *, refresh: bool = False) -> dict[str, Any]:
        """Ensure a tool exists in Aave's live inventory and return its schema."""
        inventory = await self.list_tools(refresh=refresh)
        item = inventory.get(tool) if isinstance(inventory, dict) else None
        if not isinstance(item, dict):
            raise AaveMCPError(
                f"Aave MCP tool {tool!r} is not present in the live tools/list inventory"
            )
        return item

    async def follow_next_actions(
        self,
        result: Any,
        *,
        user: str,
        version: str = "v4",
        max_actions: int = 3,
    ) -> dict[str, Any]:
        """Follow safe, read-only next_actions returned by Aave MCP.

        The Aave response envelope may instruct the caller to run a follow-up
        such as get_user_summary after an action preview. Only read tools are
        followed here; no state-changing tool is ever auto-executed.
        """
        next_actions = result.get("next_actions") if isinstance(result, dict) else []
        if not isinstance(next_actions, list):
            return {"results": [], "skipped": []}

        followups: list[dict[str, Any]] = []
        skipped: list[str] = []

        for raw in next_actions[: max(0, int(max_actions))]:
            text = str(raw).strip().lower()
            if text.startswith("get_user_summary"):
                try:
                    followups.append({
                        "action": raw,
                        "tool": "get_user_summary",
                        "result": await self.get_user_summary(user=user, version=version),
                    })
                except Exception as exc:
                    followups.append({
                        "action": raw,
                        "tool": "get_user_summary",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
            elif text.startswith("get_user_positions"):
                try:
                    followups.append({
                        "action": raw,
                        "tool": "get_user_positions",
                        "result": await self.get_user_positions(user=user, version=version),
                    })
                except Exception as exc:
                    followups.append({
                        "action": raw,
                        "tool": "get_user_positions",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
            elif text.startswith("get_user_rewards"):
                try:
                    followups.append({
                        "action": raw,
                        "tool": "get_user_rewards",
                        "result": await self.call(
                            "get_user_rewards",
                            {"user": user, "version": version},
                        ),
                    })
                except Exception as exc:
                    followups.append({
                        "action": raw,
                        "tool": "get_user_rewards",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
            else:
                skipped.append(str(raw))

        return {"results": followups, "skipped": skipped}

    async def get_user_rewards(self, *, user: str, version: str = "all") -> Any:
        return await self.call("get_user_rewards", {"user": user, "version": version})

    async def preview_action(self, **arguments: Any) -> Any:
        """Simulate a protocol action without executing it."""
        return await self.call("preview_action", arguments)

    async def prepare_action(self, *, simulation: dict[str, Any], **arguments: Any) -> Any:
        """Prepare an unsigned action only after a caller supplies a clean simulation."""
        if not isinstance(simulation, dict):
            raise ValueError("simulation must be the preview_action result")
        warnings = simulation.get("warnings") or []
        if any(isinstance(w, dict) and str(w.get("level", "")).lower() == "error" for w in warnings):
            raise AaveMCPError("preview_action returned an error warning; refusing to build")
        if not simulation.get("_dragon_simulation_ok", True):
            raise AaveMCPError("simulation was not marked successful")
        return await self.call("prepare_action", arguments)

    async def get_user_positions(self, *, user: str, version: str = "all") -> Any:
        return await self.call("get_user_positions", {"user": user, "version": version})

    async def get_user_summary(self, *, user: str, version: str = "all") -> Any:
        return await self.call("get_user_summary", {"user": user, "version": version})

    async def get_position_items(self, **arguments: Any) -> Any:
        """Fetch V4 position-level supply/borrow items using the live MCP schema.

        Keyword arguments are passed through unchanged because Aave's live
        tools/list response is authoritative for this tool's exact selectors.
        """
        return await self.call("get_position_items", arguments)

    async def get_transaction_processed(self, *, transaction_hash: str) -> Any:
        return await self.call("get_transaction_processed", {"transactionHash": transaction_hash, "version": "v4"})

    async def get_sgho_vault(self, *, user: str | None = None) -> Any:
        args: dict[str, Any] = {"version": "v3"}
        if user:
            args["user"] = user
        return await self.call("get_sgho_vault", args)

    async def vault_set_fee(
        self,
        *,
        chain_id: int,
        vault: str,
        new_fee_percent: Decimal | str | float,
    ) -> Any:
        """Prepare an unsigned Aave Earn Vault fee-update transaction.

        The Aave documentation requires a minimum performance fee of 10%.
        This method only prepares the transaction; Dragon never signs or
        broadcasts it.
        """
        fee = Decimal(str(new_fee_percent))
        if not fee.is_finite() or fee < Decimal("10") or fee > Decimal("100"):
            raise ValueError("Aave Earn Vault performance fee must be between 10% and 100%")
        return await self.call(
            "vaultSetFee",
            {
                "chainId": int(chain_id),
                "vault": vault,
                "newFee": str(fee),
            },
        )

    async def vault_set_fee_request(
        self,
        *,
        chain_id: int,
        vault: str,
        new_fee_percent: Decimal | str | float,
    ) -> dict[str, Any]:
        """Return the unsigned transaction fields for the vaultSetFee operation."""
        result = await self.vault_set_fee(
            chain_id=chain_id,
            vault=vault,
            new_fee_percent=new_fee_percent,
        )
        data = _unwrap_result(result)

        # Accept either a direct transaction object or MCP/GraphQL-style nesting.
        tx = data
        if isinstance(data, dict):
            for key in ("vaultSetFee", "data", "transaction", "result"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    tx = nested
                    break

        required = ("to", "from", "data", "value", "chainId")
        if not isinstance(tx, dict) or any(key not in tx for key in required):
            raise AaveMCPError("Aave vaultSetFee response did not contain a complete TransactionRequest")
        return {key: tx[key] for key in required}


    async def get_markets(
        self,
        version: str = "all",
        symbols: list[str] | None = None,
        user: str | None = None,
    ) -> Any:
        args: dict[str, Any] = {"version": version}
        if symbols:
            args["symbols"] = symbols
        if user:
            args["user"] = user
        return await self.call("get_markets", args)

    async def get_chains(self, version: str = "all") -> Any:
        return await self.call("get_chains", {"version": version})

    async def get_reserve_details(
        self,
        version: str,
        reserve_id: str | None = None,
        market: str | None = None,
        token: str | None = None,
        chain_id: int | None = None,
        user: str | None = None,
    ) -> Any:
        args: dict[str, Any] = {"version": version}
        if reserve_id is not None:
            args["reserveId"] = reserve_id
        if market is not None:
            args["market"] = market
        if token is not None:
            args["token"] = token
        if chain_id is not None:
            args["chainId"] = chain_id
        if user is not None:
            args["user"] = user
        return await self.call("get_reserve_details", args)


def _candidate_market_dicts(payload: Any) -> list[dict[str, Any]]:
    """Extract Aave reserve/market rows across current and legacy MCP shapes.

    Token identity may appear as underlyingToken or token, and market metadata
    may be carried by a parent object. Do not require one nesting shape.
    """
    payload = _unwrap_result(payload)
    candidates: list[dict[str, Any]] = []
    seen_ids: set[int] = set()

    for item in _walk_dicts(payload):
        if not isinstance(item, dict) or id(item) in seen_ids:
            continue

        token = item.get("underlyingToken")
        if not isinstance(token, dict):
            token = item.get("token")
        symbol = token.get("symbol") if isinstance(token, dict) else item.get("symbol")

        reserve_like = any(key in item for key in (
            "reserveId", "reserve_id", "reserve", "summary", "settings",
            "status", "canSupply", "canBorrow", "supplyApy", "supplyAPY",
            "supplyRate", "availableLiquidity", "liquidity", "isFrozen",
            "isPaused", "suppliable",
        ))
        market_like = isinstance(item.get("market"), dict) or any(
            key in item for key in ("marketAddress", "market_address", "chainId", "chain_id")
        )

        if symbol and (reserve_like or market_like):
            candidates.append(item)
            seen_ids.add(id(item))

    if not candidates:
        for item in _walk_dicts(payload):
            if not isinstance(item, dict):
                continue
            symbol = _deep_get(item, "symbol")
            if symbol is None:
                token = _deep_get(item, "token", "underlyingToken")
                symbol = _deep_get(token, "symbol") if isinstance(token, dict) else None
            if symbol is None:
                continue
            if any(_deep_get(item, key) is not None for key in (
                "reserveId", "reserve_id", "supplyApy", "supplyAPY",
                "supplyRate", "availableLiquidity", "liquidity",
            )):
                candidates.append(item)

    return candidates

def parse_markets(payload: Any, stablecoins: tuple[str, ...] = DEFAULT_STABLECOINS) -> list[AaveMarketSnapshot]:
    allowed = {s.upper() for s in stablecoins}
    rows: list[AaveMarketSnapshot] = []
    seen: set[tuple[str, str, str]] = set()

    for item in _candidate_market_dicts(payload):
        token_obj = item.get("underlyingToken") if isinstance(item.get("underlyingToken"), dict) else None
        symbol = token_obj.get("symbol") if token_obj else _deep_get(item, "symbol")
        if isinstance(symbol, dict):
            symbol = symbol.get("symbol") or symbol.get("name")
        symbol = str(symbol or "").strip()
        if symbol.upper() not in allowed:
            continue

        version = str(_deep_get(item, "version", "protocolVersion", "aaveVersion") or "").lower()
        if version not in {"v3", "v4"}:
            # get_markets(version=all) may inherit the version on a parent object;
            # leave it explicit as unknown rather than inventing a version.
            version = "unknown"

        market_obj = item.get("market") if isinstance(item.get("market"), dict) else {}
        chain_id_value = market_obj.get("chainId") if market_obj else _deep_get(item, "chainId", "chain_id")
        try:
            chain_id = int(chain_id_value) if chain_id_value is not None else None
        except (TypeError, ValueError):
            chain_id = None

        chain = str(
            market_obj.get("chain")
            or market_obj.get("chainName")
            or market_obj.get("network")
            or _deep_get(item, "chain", "chainName", "network")
            or (chain_id or "unknown")
        )
        reserve_id = _deep_get(item, "reserveId", "reserve_id")
        market_address = market_obj.get("address") if market_obj else _deep_get(item, "marketAddress", "market_address")
        token_address = token_obj.get("address") if token_obj else _deep_get(item, "tokenAddress", "underlyingTokenAddress")
        token_name = token_obj.get("name") if token_obj else _deep_get(item, "tokenName", "underlyingTokenName")
        key = (version, str(chain_id), f"{symbol}:{reserve_id}")
        if key in seen:
            continue
        seen.add(key)

        rewards = _deep_get(item, "rewards") or []
        if isinstance(rewards, dict):
            rewards = [rewards]

        row = AaveMarketSnapshot(
            version=version,
            chain_id=chain_id,
            chain=chain,
            symbol=symbol,
            reserve_id=str(reserve_id) if reserve_id is not None else None,
            market_address=str(market_address) if market_address is not None else None,
            underlying_token_address=str(token_address) if token_address is not None else None,
            underlying_token_name=str(token_name) if token_name is not None else None,
            supply_apy_pct=_pct(_deep_get(item, "supplyApy", "supplyAPY", "supplyRate", "summary")),
            available_liquidity=_decimal(_deep_get(item, "availableLiquidity", "available_liquidity", "liquidity")),
            utilization_pct=_pct(_deep_get(item, "utilization", "utilizationPct", "utilizationPercentage")),
            supply_cap=_decimal(_deep_get(item, "supplyCap", "supply_cap")),
            borrow_cap=_decimal(_deep_get(item, "borrowCap", "borrow_cap")),
            frozen=_bool(_deep_get(item, "isFrozen", "frozen")),
            paused=_bool(_deep_get(item, "isPaused", "paused")),
            can_supply=(
                _bool(_deep_get(item, "canSupply"))
                if _deep_get(item, "canSupply") is not None else None
            ),
            can_use_as_collateral=(
                _bool(_deep_get(item, "canUseAsCollateral"))
                if _deep_get(item, "canUseAsCollateral") is not None else None
            ),
            suppliable=_decimal(_deep_get(item, "suppliable", "userState")),
            rewards=tuple(r for r in rewards if isinstance(r, dict)),
            raw=item,
        )
        rows.append(row)

    return rows


def rank_stablecoin_supply(
    rows: list[AaveMarketSnapshot],
    *,
    require_can_supply: bool = True,
) -> list[AaveMarketSnapshot]:
    eligible = [
        row for row in rows
        if not row.frozen
        and not row.paused
        and row.supply_apy_pct is not None
        and (not require_can_supply or row.can_supply is not False)
        and (row.suppliable is None or row.suppliable > 0)
    ]
    return sorted(
        eligible,
        key=lambda row: (
            row.displayed_apy_pct if row.displayed_apy_pct is not None else Decimal("-1"),
            row.supply_apy_pct if row.supply_apy_pct is not None else Decimal("-1"),
        ),
        reverse=True,
    )


async def prepare_supply(
    client: AaveMCPClient,
    *,
    sender: str,
    reserve: str,
    chain_id: int,
    amount: str,
    enable_collateral: bool = False,
) -> dict[str, Any]:
    """Preview and prepare an unsigned Aave V4 supply action."""
    request: dict[str, Any] = {
        "sender": sender,
        "reserve": reserve,
        "chainId": int(chain_id),
        "amount": {"erc20": {"value": canonical_human_amount(str(amount))}},
    }
    if enable_collateral:
        request["enableCollateral"] = True

    preview = await client.preview_action(action={"supply": request})
    if not isinstance(preview, dict):
        raise AaveMCPError("Aave supply preview returned an unexpected response")
    warnings = preview.get("warnings") or []
    if any(
        isinstance(w, dict) and str(w.get("level", "")).lower() == "error"
        for w in warnings
    ):
        raise AaveMCPError("Aave supply preview returned an error warning")

    simulated = dict(preview)
    simulated["_dragon_simulation_ok"] = True
    plan = await client.prepare_action(
        simulation=simulated,
        action={"supply": request},
    )
    return {
        "simulation": preview,
        "execution": plan,
        "unsigned": True,
        "signer": sender,
    }

async def prepare_withdraw(
    client: AaveMCPClient,
    *,
    sender: str,
    reserve: str,
    amount: str | None = None,
    maximum: bool = False,
    withdrawable: str | None = None,
    reserve_paused: bool | None = None,
) -> dict[str, Any]:
    """Preview and prepare an unsigned Aave V4 withdraw action.

    Uses Aave's exact request shape:
      amount.erc20.value.exact for a fixed withdrawal, or
      amount.erc20.value.max for a full withdrawal.

    A caller can pass live position data (withdrawable + paused) to enforce
    local preflight checks before calling the official MCP preview.
    """
    if maximum and amount is not None:
        raise ValueError("amount and maximum=True are mutually exclusive")
    if not maximum and amount is None:
        raise ValueError("amount is required unless maximum=True")
    if reserve_paused is True:
        raise AaveMCPError("cannot withdraw while the Aave reserve is paused")

    if amount is not None:
        requested = parse_human_amount(str(amount))
        if not requested.is_finite() or requested <= 0:
            raise ValueError("withdraw amount must be positive and finite")
        if withdrawable is not None and requested > Decimal(str(withdrawable)):
            raise AaveMCPError(
                f"withdraw amount {requested} exceeds live withdrawable amount {withdrawable}"
            )
        value: dict[str, Any] = {"exact": canonical_human_amount(str(amount))}
    else:
        value = {"max": True}

    request = {
        "sender": sender,
        "reserve": reserve,
        "amount": {"erc20": {"value": value}},
    }

    preview = await client.preview_action(action={"withdraw": request})
    if not isinstance(preview, dict):
        raise AaveMCPError("Aave withdraw preview returned an unexpected response")

    warnings = preview.get("warnings") or []
    if any(
        isinstance(w, dict) and str(w.get("level", "")).lower() == "error"
        for w in warnings
    ):
        raise AaveMCPError("Aave withdraw preview returned an error warning")

    simulated = dict(preview)
    simulated["_dragon_simulation_ok"] = True
    execution = await client.prepare_action(
        simulation=simulated,
        action={"withdraw": request},
    )
    return {
        "simulation": preview,
        "execution": execution,
        "unsigned": True,
        "signer": sender,
    }


async def fetch_stablecoin_yields(client: AaveMCPClient | None = None, stablecoins: tuple[str, ...] = DEFAULT_STABLECOINS) -> list[AaveMarketSnapshot]:
    client = client or AaveMCPClient()
    payload = await client.get_markets(version="all", symbols=list(stablecoins))
    return parse_markets(payload, stablecoins=stablecoins)


async def fetch_best_stablecoin_yields(client: AaveMCPClient | None = None, stablecoins: tuple[str, ...] = DEFAULT_STABLECOINS, limit: int = 20) -> list[dict[str, Any]]:
    rows = await fetch_stablecoin_yields(client, stablecoins=stablecoins)
    return [row.as_dict() for row in rank_stablecoin_supply(rows)[:max(1, limit)]]
