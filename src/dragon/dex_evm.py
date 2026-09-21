"""Generic multi-chain EVM DEX adapter.

One adapter instance serves every enabled chain and venue. It speaks the three
AMM interfaces the registry declares:

* ``v2``     — ``getAmountsOut`` / ``swapExactTokensForTokens``
* ``v3``     — QuoterV2 ``quoteExactInputSingle`` / ``exactInputSingle``
* ``stable`` — Aerodrome-style router with a ``stable`` hop flag
* ``lb``     — Trader Joe Liquidity Book ``findBestPathFromAmountIn``

The adapter is quote-first: every call validates the chain, the venue and the
sell amount, and it fails closed when an RPC or venue is unavailable.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import random
from decimal import Decimal

from web3 import Web3

from .chains import ChainSpec, get_spec, rpc_urls, env_chain_ids
from .dex import DexQuote
from .dex_0x import DexExecution
from .venues import Venue, venues_for


class RpcRateLimitError(RuntimeError):
    """Raised when every configured RPC attempt for a chain is rate-limited."""


MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"

V2_ROUTER_ABI = [
    {"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}], "name": "getAmountsOut", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"internalType": "address[]", "name": "path", "type": "address[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"},
]

V3_QUOTER_ABI = [
    {"inputs": [{"components": [{"internalType": "address", "name": "tokenIn", "type": "address"}, {"internalType": "address", "name": "tokenOut", "type": "address"}, {"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}, {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}], "internalType": "struct IQuoterV2.QuoteExactInputSingleParams", "name": "params", "type": "tuple"}], "name": "quoteExactInputSingle", "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}, {"internalType": "uint160", "name": "sqrtPriceX96After", "type": "uint160"}, {"internalType": "uint32", "name": "initializedTicksCrossed", "type": "uint32"}, {"internalType": "uint256", "name": "gasEstimate", "type": "uint256"}], "stateMutability": "nonpayable", "type": "function"},
]

V3_ROUTER_ABI = [
    {"inputs": [{"components": [{"internalType": "address", "name": "tokenIn", "type": "address"}, {"internalType": "address", "name": "tokenOut", "type": "address"}, {"internalType": "uint24", "name": "fee", "type": "uint24"}, {"internalType": "address", "name": "recipient", "type": "address"}, {"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"}, {"internalType": "uint160", "name": "sqrtPriceLimitX96", "type": "uint160"}], "internalType": "struct IV3SwapRouter.ExactInputSingleParams", "name": "params", "type": "tuple"}], "name": "exactInputSingle", "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
    {"inputs": [{"components": [{"internalType": "bytes", "name": "path", "type": "bytes"}, {"internalType": "address", "name": "recipient", "type": "address"}, {"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"}], "internalType": "struct IV3SwapRouter.ExactInputParams", "name": "params", "type": "tuple"}], "name": "exactInput", "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}], "stateMutability": "payable", "type": "function"},
]

# Aerodrome/Velodrome-style router: V2 shapes plus a per-hop stable flag.
STABLE_ROUTER_ABI = [
    {"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"components": [{"internalType": "address", "name": "from", "type": "address"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "bool", "name": "stable", "type": "bool"}, {"internalType": "address", "name": "factory", "type": "address"}], "internalType": "struct IRouter.Route[]", "name": "routes", "type": "tuple[]"}], "name": "getAmountsOut", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"internalType": "uint256", "name": "amountIn", "type": "uint256"}, {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"}, {"components": [{"internalType": "address", "name": "from", "type": "address"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "bool", "name": "stable", "type": "bool"}, {"internalType": "address", "name": "factory", "type": "address"}], "internalType": "struct IRouter.Route[]", "name": "routes", "type": "tuple[]"}, {"internalType": "address", "name": "to", "type": "address"}, {"internalType": "uint256", "name": "deadline", "type": "uint256"}], "name": "swapExactTokensForTokens", "outputs": [{"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [], "name": "defaultFactory", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

LB_QUOTER_ABI = [
    {"inputs": [{"internalType": "address", "name": "tokenIn", "type": "address"}, {"internalType": "address", "name": "tokenOut", "type": "address"}, {"internalType": "uint128", "name": "amountIn", "type": "uint128"}], "name": "findBestPathFromAmountIn", "outputs": [{"components": [{"internalType": "uint16[]", "name": "binSteps", "type": "uint16[]"}, {"internalType": "address[]", "name": "versions", "type": "address[]"}, {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}, {"internalType": "address[]", "name": "virtualAmountsWithoutSlippage", "type": "address[]"}, {"internalType": "address[]", "name": "pairs", "type": "address[]"}], "internalType": "struct LBRouter.Quote", "name": "quote", "type": "tuple"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "factory", "outputs": [{"internalType": "address", "name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
]

MULTICALL3_ABI = [
    {"inputs": [{"components": [{"internalType": "address", "name": "target", "type": "address"}, {"internalType": "bool", "name": "allowFailure", "type": "bool"}, {"internalType": "bytes", "name": "callData", "type": "bytes"}], "internalType": "struct Multicall3.Call3[]", "name": "calls", "type": "tuple[]"}], "name": "aggregate3", "outputs": [{"components": [{"internalType": "bool", "name": "success", "type": "bool"}, {"internalType": "bytes", "name": "returnData", "type": "bytes"}], "internalType": "struct Multicall3.Result[]", "name": "returnData", "type": "tuple[]"}], "stateMutability": "payable", "type": "function"},
]


class RpcPool:
    """Bounded, fail-fast RPC pool for a single chain.

    Rotation prefers keyed (paid) endpoints, honours per-endpoint cool-downs and
    never blocks a quote for longer than its deadline.
    """

    def __init__(self, chain: ChainSpec, urls: list[str], timeout: float | None = None):
        if not urls:
            raise RpcRateLimitError(f"no RPC endpoint configured for chain {chain.chain_id} ({chain.name})")
        self.chain = chain
        self.urls = urls
        self._keyed = [self._is_keyed(url) for url in urls]
        self._has_keyed = any(self._keyed)
        configured = float(os.getenv("DEX_RPC_TIMEOUT_SECONDS", "0.4")) if timeout is None else float(timeout)
        ceiling = 2.0 if self._has_keyed else 1.2
        # RPC HTTP timeout must fit inside Dragon's hard quote deadline.
        # The previous 0.9s default exceeded the 500ms quote budget, causing
        # RpcPool.call() to reject every request before it was sent.
        hard_deadline = max(100.0, float(os.getenv("DEX_HARD_QUOTE_DEADLINE_MS", "500"))) / 1000.0
        deadline_margin = max(0.05, min(0.15, hard_deadline * 0.20))
        timeout_ceiling = max(0.2, hard_deadline - deadline_margin)
        self.timeout = max(0.2, min(ceiling, configured, timeout_ceiling))
        self._index = 0
        self._failures = [0] * len(urls)
        self._cooldown_until = [0.0] * len(urls)
        self._success = [0] * len(urls)
        self._inflight = [0] * len(urls)
        self._latency_ms = [0.0] * len(urls)
        self._rate_limits = [0] * len(urls)
        self._provider = [self._provider_name(url) for url in urls]
        self._provider_inflight: dict[str, int] = {}
        self._provider_cooldown_until: dict[str, float] = {}
        self._provider_max_inflight = max(1, int(os.getenv("DEX_RPC_PROVIDER_MAX_INFLIGHT", "2")))
        self._errors = [0] * len(urls)
        self._selftest = ["untested"] * len(urls)
        self._lock = threading.Lock()
        self._clients: dict[str, tuple] = {}
        self.w3: Web3 | None = None
        self.url = ""
        errors = []
        for idx, url in enumerate(urls):
            try:
                self._bind(idx)
                break
            except Exception as exc:
                errors.append(f"url={url} {type(exc).__name__}: {exc}")
                self._cooldown_until[idx] = time.monotonic() + min(5.0, 0.5 * (idx + 1))
                logging.warning("RPC startup failed chain=%s index=%s host=%s error=%s", chain.name, idx, self._host(url), exc)
        if self.w3 is None:
            raise RpcRateLimitError(f"all RPC endpoints failed for {chain.name}: " + " | ".join(errors))

    @staticmethod
    def _provider_name(url: str) -> str:
        host = RpcPool._host(url).lower()
        if "drpc.org" in host or "drpc.live" in host:
            return "drpc"
        if "publicnode" in host:
            return "publicnode"
        if "llamarpc" in host:
            return "llamarpc"
        if "1rpc" in host:
            return "1rpc"
        if "alchemy" in host:
            return "alchemy"
        if "infura" in host:
            return "infura"
        if "quicknode" in host or "quiknode" in host:
            return "quicknode"
        return host or "unknown"

    @staticmethod
    def _host(url: str) -> str:
        if not url:
            return "none"
        try:
            return url.split("//", 1)[-1].split("/", 1)[0] or "unparseable"
        except Exception:
            return "unparseable"

    @staticmethod
    def _is_keyed(url: str) -> bool:
        if not re.search(r"/(v2|v3)/[A-Za-z0-9_-]{8,}", url):
            return False
        tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
        return tail not in {"docs-demo", "demo", "your-key", "your_key", "yourkey"}

    def _bind(self, idx: int) -> None:
        url = self.urls[idx]
        cached = self._clients.get(url)
        if cached is None:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self.timeout}))
            if not w3.is_connected():
                raise RuntimeError(f"cannot connect to {self.chain.name} RPC: {url}")
            got = int(w3.eth.chain_id)
            if got != self.chain.chain_id:
                raise RuntimeError(f"{self.chain.name} RPC returned chain {got}, expected {self.chain.chain_id}")
            cached = (w3,)
            self._clients[url] = cached
        self.w3 = cached[0]
        self.url = url
        self._index = idx

    @staticmethod
    def _transient(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(x in msg for x in ("429", "too many requests", "rate limit", "usage limit", "timeout", "timed out", "temporarily unavailable", "service unavailable", "connection reset", "connection aborted", "-32001", "-32005"))

    @staticmethod
    def _only_rpc_failures(errors: list[str]) -> bool:
        return bool(errors) and all(
            "RpcRateLimitError" in e or "429" in e or "rate limit" in e.lower() or "deadline exhausted" in e.lower()
            for e in errors
        )

    def call(self, fn, deadline: float | None = None):
        last_exc = None
        attempted: set[int] = set()
        max_attempts = max(1, min(len(self.urls), int(os.getenv("DEX_RPC_MAX_ATTEMPTS", "4"))))
        for _ in range(max_attempts):
            now = time.monotonic()
            with self._lock:
                ready = [
                    i for i in range(len(self.urls))
                    if i not in attempted
                    and self._cooldown_until[i] <= now
                    and self._provider_cooldown_until.get(self._provider[i], 0.0) <= now
                    and self._provider_inflight.get(self._provider[i], 0) < self._provider_max_inflight
                ]
                if ready:
                    keyed_ready = [i for i in ready if self._keyed[i]]
                    candidates = keyed_ready or ready
                    idx = min(candidates, key=lambda i: (
                        self._provider_inflight.get(self._provider[i], 0),
                        self._inflight[i], self._rate_limits[i],
                        self._failures[i], self._latency_ms[i] or 0.0,
                    ))
                    self._inflight[idx] += 1
                    provider = self._provider[idx]
                    self._provider_inflight[provider] = self._provider_inflight.get(provider, 0) + 1
                    self._index = (idx + 1) % len(self.urls)
                else:
                    idx = -1
                    provider = ""
            if idx < 0:
                if deadline is not None and time.monotonic() + self.timeout > deadline:
                    break
                remaining = [i for i in range(len(self.urls)) if i not in attempted]
                if not remaining:
                    break
                idx = min(remaining, key=lambda i: self._cooldown_until[i])
                provider = self._provider[idx]
                attempted.add(idx)
                continue
            attempted.add(idx)
            started = time.monotonic()
            try:
                if self.url != self.urls[idx]:
                    self._bind(idx)
                if deadline is not None and time.monotonic() + self.timeout > deadline:
                    raise RpcRateLimitError("RPC call skipped: quote deadline exhausted")
                result = fn(self.w3)
                elapsed_ms = (time.monotonic() - started) * 1000.0
                with self._lock:
                    self._failures[idx] = 0
                    self._success[idx] += 1
                    self._latency_ms[idx] = elapsed_ms if not self._latency_ms[idx] else (0.8 * self._latency_ms[idx] + 0.2 * elapsed_ms)
                return result
            except Exception as exc:
                last_exc = exc
                if not self._transient(exc):
                    with self._lock:
                        self._errors[idx] += 1
                    raise
                with self._lock:
                    self._failures[idx] += 1
                    self._errors[idx] += 1
                    is_429 = any(x in str(exc).lower() for x in ("429", "too many requests", "rate limit"))
                    if is_429:
                        self._rate_limits[idx] += 1
                    base = 1.0 if self._keyed[idx] else 3.0
                    cap = 8.0 if self._keyed[idx] else 20.0
                    backoff = min(cap, base * (2 ** min(self._failures[idx] - 1, 2))) + random.uniform(0.05, 0.35)
                    self._cooldown_until[idx] = time.monotonic() + backoff
                    if is_429:
                        self._provider_cooldown_until[provider] = max(self._provider_cooldown_until.get(provider, 0.0), time.monotonic() + backoff)
                logging.warning("RPC transient error chain=%s index=%s provider=%s cooldown=%.1fs error=%s", self.chain.name, idx, provider, backoff, exc)
            finally:
                with self._lock:
                    self._inflight[idx] = max(0, self._inflight[idx] - 1)
                    self._provider_inflight[provider] = max(0, self._provider_inflight.get(provider, 1) - 1)
        if all(self._cooldown_until[i] > time.monotonic() for i in range(len(self.urls))):
            reason = "all RPC endpoints are in cool-down; quote deadline too short to wait"
        elif not attempted:
            reason = "no RPC endpoint was eligible within the quote deadline"
        else:
            reason = f"all RPC attempts failed: {last_exc}"
        raise RpcRateLimitError(reason)

    def status(self) -> dict:
        return {
            "chain": self.chain.name,
            "active_host": self._host(self.url),
            "endpoints": [
                {
                    "host": self._host(url),
                    "keyed": bool(self._keyed[i]),
                    "selftest": self._selftest[i],
                    "success": self._success[i],
                    "errors": self._errors[i],
                    "rate_limits": self._rate_limits[i],
                    "inflight": self._inflight[i],
                    "latency_ms": round(self._latency_ms[i], 2),
                    "provider": self._provider[i],
                    "cooling_down": self._cooldown_until[i] > time.monotonic() or self._provider_cooldown_until.get(self._provider[i], 0.0) > time.monotonic(),
                }
                for i, url in enumerate(self.urls)
            ],
        }


class _EvmDexCore:
    """Chain-aware, multi-venue EVM DEX adapter."""

    def __init__(self, chain_ids: list[int] | None = None):
        self._pools: dict[int, RpcPool] = {}
        self._venues: dict[int, dict[str, Venue]] = {}
        self._contracts: dict[tuple[int, str], object] = {}
        self._quote_cache: dict[tuple, tuple[float, tuple]] = {}
        self._pair_capability_cache: dict[tuple[int, str, str, str], float] = {}
        self._pair_capability_ttl = max(2.0, float(os.getenv("DEX_PAIR_CAPABILITY_TTL_SECONDS", "15")))
        self._native_rate_cache: dict[tuple, tuple[float, Decimal]] = {}
        self._gas_cache: dict[int, tuple[float, int]] = {}
        self._quote_cache_ttl = max(0.0, float(os.getenv("DEX_QUOTE_CACHE_SECONDS", "0.25")))
        self.deadline_seconds = max(5, int(os.getenv("DEX_DEADLINE_SECONDS", "20")))
        self.multicall_enabled = os.getenv("DEX_MULTICALL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.route_intermediates_enabled = os.getenv("DEX_ROUTE_INTERMEDIATES_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.gas_limit = max(100_000, int(os.getenv("DEX_GAS_LIMIT", "300000")))
        self.flash_loan_pool = os.getenv("DEX_AAVE_POOL_ADDRESS", "").strip()

        wanted = chain_ids if chain_ids is not None else env_chain_ids()
        for cid in wanted:
            spec = get_spec(cid)
            if spec is None or spec.family != "evm":
                continue
            urls = rpc_urls(spec)
            if not urls:
                logging.warning("chain %s enabled but no RPC configured via %s; skipping", spec.name, spec.rpc_env)
                continue
            try:
                pool = RpcPool(spec, urls)
            except RpcRateLimitError as exc:
                logging.warning("chain %s unavailable at startup: %s", spec.name, exc)
                continue
            self._pools[cid] = pool
            venues = venues_for(cid)
            self._venues[cid] = {v.name: v for v in venues}
            logging.info(
                "EVM chain ready chain=%s id=%s host=%s venues=%s",
                spec.name, cid, pool._host(pool.url), [v.name for v in venues],
            )
        if not self._pools:
            raise RpcRateLimitError("no EVM chain could be initialized; check DEX_CHAINS and RPC env vars")

    def _flash_loan_pool_address(self, chain_id: int) -> str:
        """Resolve the configured Aave V3 Pool per chain, with a global fallback."""
        return os.getenv(f"DEX_AAVE_POOL_{int(chain_id)}", "").strip() or self.flash_loan_pool


    # --- helpers -----------------------------------------------------------

    @staticmethod
    def _addr(value: str) -> str:
        return Web3.to_checksum_address(value)

    def _pool(self, chain_id: int) -> RpcPool:
        pool = self._pools.get(int(chain_id))
        if pool is None:
            raise ValueError(f"chain {chain_id} is not enabled on this adapter")
        return pool

    def _contract(self, chain_id: int, address: str, abi: list):
        key = (int(chain_id), address.lower())
        cached = self._contracts.get(key)
        if cached is None:
            w3 = self._pool(chain_id).w3
            cached = w3.eth.contract(address=self._addr(address), abi=abi)
            self._contracts[key] = cached
        return cached

    def _rpc(self, chain_id: int, fn, deadline=None):
        return self._pool(chain_id).call(fn, deadline=deadline)

    def _gas_price(self, chain_id: int, deadline=None) -> int:
        now = time.monotonic()
        cached = self._gas_cache.get(chain_id)
        if cached and now - cached[0] < 1.0:
            return cached[1]
        price = int(self._rpc(chain_id, lambda w3: w3.eth.gas_price, deadline=deadline))
        self._gas_cache[chain_id] = (now, price)
        return price

    # --- interface ---------------------------------------------------------

    def sources(self, chain_id: int) -> tuple[str, ...]:
        return tuple(self._venues.get(int(chain_id), {}).keys())

    def chains(self) -> tuple[int, ...]:
        return tuple(self._pools.keys())

    def rpc_status(self) -> list[dict]:
        return [pool.status() for pool in self._pools.values()]

    def clone_for_concurrent_quotes(self):
        return EvmDexAdapter(chain_ids=list(self._pools.keys()))

    def close(self) -> None:
        for pool in self._pools.values():
            provider = getattr(pool.w3, "provider", None)
            if provider and hasattr(provider, "disconnect"):
                provider.disconnect()

    def flash_loan_fee_bps(self, chain_id: int = 8453) -> Decimal:
        pool_address = self._flash_loan_pool_address(chain_id)
        if not pool_address:
            raise RuntimeError(f"Aave V3 Pool address is not configured for chain {chain_id}")
        if not Web3.is_address(pool_address):
            raise ValueError(f"invalid Aave V3 Pool address for chain {chain_id}")
        abi = [{"inputs": [], "name": "FLASHLOAN_PREMIUM_TOTAL", "outputs": [{"internalType": "uint128", "name": "", "type": "uint128"}], "stateMutability": "view", "type": "function"}]
        raw = Decimal(str(self._rpc(chain_id, lambda w3: w3.eth.contract(address=self._addr(pool_address), abi=abi).functions.FLASHLOAN_PREMIUM_TOTAL().call())))
        if raw < 0 or raw > Decimal("1000"):
            raise RuntimeError(f"invalid Aave flash-loan premium: {raw} bps")
        return raw

    def native_to_quote_rate(self, *, chain_id: int, quote_token: str, sell_amount_native: int, taker: str) -> Decimal:
        spec = get_spec(chain_id)
        if spec is None:
            return Decimal("0")
        key = (int(chain_id), quote_token.lower())
        cached = self._native_rate_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] < 15.0:
            return cached[1]
        sources = self.sources(chain_id)
        if not sources:
            return Decimal("0")
        errors: list[str] = []
        for source in sources:
            try:
                quote, _ = self.quote_single_source(
                    chain_id=chain_id, sell_token=spec.wrapped_native, buy_token=quote_token,
                    sell_amount=sell_amount_native, taker=taker, source=source, slippage_bps=50, probe=True,
                )
                rate = quote.buy_amount / quote.sell_amount
                self._native_rate_cache[key] = (now, rate)
                return rate
            except Exception as exc:
                errors.append(f"{source}: {type(exc).__name__}: {exc}")
        raise RuntimeError("no venue could price native gas token; " + " | ".join(errors[-3:]))

    # --- venue quotes ------------------------------------------------------

    def _route_paths(self, spec: ChainSpec, token_in: str, token_out: str) -> list[tuple[str, ...]]:
        paths = [(token_in, token_out)]
        if self.route_intermediates_enabled:
            mid = spec.wrapped_native
            if mid.lower() not in {token_in.lower(), token_out.lower()}:
                paths.append((token_in, mid, token_out))
        return paths

    def _v2_quote(self, chain_id: int, venue: Venue, token_in: str, token_out: str, amount: int, deadline=None):
        spec = get_spec(chain_id)
        router = self._contract(chain_id, venue.router, V2_ROUTER_ABI)
        cache_key = ("v2", chain_id, venue.name, token_in.lower(), token_out.lower(), amount)
        now = time.monotonic()
        cached = self._quote_cache.get(cache_key)
        if cached and now - cached[0] < self._quote_cache_ttl:
            return cached[1]
        errors: list[str] = []
        best = None
        for path in self._route_paths(spec, token_in, token_out):
            try:
                addrs = [self._addr(t) for t in path]
                amounts = self._rpc(
                    chain_id,
                    lambda w3, r=router, a=addrs: r.functions.getAmountsOut(amount, a).call(),
                    deadline=deadline,
                )
                out = int(amounts[-1])
                if len(amounts) != len(path):
                    errors.append("v2 length mismatch")
                    continue
                if out > 0 and (best is None or out > best[0]):
                    best = (out, tuple(path))
            except Exception as exc:
                errors.append(f"v2 {type(exc).__name__}: {exc}")
        if best is None:
            message = f"no {venue.name} liquidity for pair; " + " | ".join(errors[-3:])
            if RpcPool._only_rpc_failures(errors):
                raise RpcRateLimitError(message)
            raise RuntimeError(message)
        self._quote_cache[cache_key] = (now, best)
        return best

    def _v3_quote(self, chain_id: int, venue: Venue, token_in: str, token_out: str, amount: int, deadline=None):
        spec = get_spec(chain_id)
        fees = venue.fee_tiers or (500, 3000, 10000)
        cache_key = ("v3", chain_id, venue.name, token_in.lower(), token_out.lower(), amount, fees)
        now = time.monotonic()
        cached = self._quote_cache.get(cache_key)
        if cached and now - cached[0] < self._quote_cache_ttl:
            return cached[1]
        quoter = self._contract(chain_id, venue.quoter, V3_QUOTER_ABI)
        errors: list[str] = []
        best = None
        for path in self._route_paths(spec, token_in, token_out):
            if len(path) == 2:
                fee_pairs = [(f,) for f in fees]
            else:
                mid_fee = [f for f in fees if f in (500, 3000, 10000)] or list(fees)
                fee_pairs = [(a, b) for a in mid_fee for b in mid_fee]
            for fee_pair in fee_pairs:
                try:
                    if len(path) == 2:
                        result = self._rpc(
                            chain_id,
                            lambda w3, q=quoter, p=path, fp=fee_pair: q.functions.quoteExactInputSingle(
                                (self._addr(p[0]), self._addr(p[1]), amount, int(fp[0]), 0)
                            ).call(),
                            deadline=deadline,
                        )
                        out = int(result[0])
                    else:
                        encoded = self._encode_v3_path(path, fee_pair)
                        result = self._rpc(
                            chain_id,
                            lambda w3, q=quoter, e=encoded: q.functions.quoteExactInput(e, amount).call()
                            if hasattr(q.functions, "quoteExactInput")
                            else q.functions.quoteExactInputSingle((self._addr(path[0]), self._addr(path[1]), amount, int(fee_pair[0]), 0)).call(),
                            deadline=deadline,
                        )
                        out = int(result[0])
                    if out > 0 and (best is None or out > best[0]):
                        best = (out, tuple(path), tuple(fee_pair))
                except Exception as exc:
                    errors.append(f"v3 {type(exc).__name__}: {exc}")
        if best is None:
            message = f"no {venue.name} liquidity for pair; " + " | ".join(errors[-3:])
            if RpcPool._only_rpc_failures(errors):
                raise RpcRateLimitError(message)
            raise RuntimeError(message)
        self._quote_cache[cache_key] = (now, best)
        return best

    def _stable_quote(self, chain_id: int, venue: Venue, token_in: str, token_out: str, amount: int, deadline=None):
        spec = get_spec(chain_id)
        router = self._contract(chain_id, venue.router, STABLE_ROUTER_ABI)
        factory = venue.factory or self._rpc(chain_id, lambda w3: router.functions.defaultFactory().call(), deadline=deadline)
        cache_key = ("stable", chain_id, venue.name, token_in.lower(), token_out.lower(), amount)
        now = time.monotonic()
        cached = self._quote_cache.get(cache_key)
        if cached and now - cached[0] < self._quote_cache_ttl:
            return cached[1]
        errors: list[str] = []
        best = None
        for path in self._route_paths(spec, token_in, token_out):
            hops = len(path) - 1
            flags = list(_stable_flag_combinations(hops))
            for combo in flags:
                route = [
                    {"from": self._addr(path[i]), "to": self._addr(path[i + 1]), "stable": bool(combo[i]), "factory": self._addr(factory)}
                    for i in range(len(path) - 1)
                ]
                try:
                    amounts = self._rpc(
                        chain_id,
                        lambda w3, r=router, rt=route: r.functions.getAmountsOut(amount, rt).call(),
                        deadline=deadline,
                    )
                    out = int(amounts[-1])
                    if out > 0 and (best is None or out > best[0]):
                        best = (out, tuple(route), factory)
                except Exception as exc:
                    errors.append(f"stable {type(exc).__name__}: {exc}")
        if best is None:
            message = f"no {venue.name} liquidity for pair; " + " | ".join(errors[-3:])
            if RpcPool._only_rpc_failures(errors):
                raise RpcRateLimitError(message)
            raise RuntimeError(message)
        self._quote_cache[cache_key] = (now, best)
        return best

    def _lb_quote(self, chain_id: int, venue: Venue, token_in: str, token_out: str, amount: int, deadline=None):
        quoter = self._contract(chain_id, venue.router, LB_QUOTER_ABI)
        cache_key = ("lb", chain_id, venue.name, token_in.lower(), token_out.lower(), amount)
        now = time.monotonic()
        cached = self._quote_cache.get(cache_key)
        if cached and now - cached[0] < self._quote_cache_ttl:
            return cached[1]
        try:
            quote = self._rpc(
                chain_id,
                lambda w3: quoter.functions.findBestPathFromAmountIn(
                    self._addr(token_in), self._addr(token_out), int(amount)
                ).call(),
                deadline=deadline,
            )
        except Exception as exc:
            message = f"no {venue.name} route for pair: {type(exc).__name__}: {exc}"
            if RpcPool._only_rpc_failures([str(exc)]):
                raise RpcRateLimitError(message)
            raise RuntimeError(message)
        amounts = quote[2]
        out = int(amounts[-1]) if amounts else 0
        if out <= 0:
            raise RuntimeError(f"no {venue.name} liquidity for pair")
        best = (out, tuple(quote[0]), tuple(quote[2]))
        self._quote_cache[cache_key] = (now, best)
        return best

    @staticmethod
    def _encode_v3_path(tokens, fees) -> str:
        data = bytearray()
        for i, token in enumerate(tokens):
            data.extend(bytes.fromhex(Web3.to_checksum_address(token)[2:]))
            if i < len(fees):
                data.extend(int(fees[i]).to_bytes(3, "big"))
        return "0x" + data.hex()





class EvmDexAdapter(_EvmDexCore):
    """Quote dispatch and execution calldata on top of the RPC/venue core."""

    def quote_single_source(self, *, chain_id: int, sell_token: str, buy_token: str, sell_amount: int, taker: str, source: str, slippage_bps: int = 50, deadline: float | None = None, probe: bool = False):
        started = time.perf_counter()
        spec = get_spec(chain_id)
        if spec is None or spec.family != "evm":
            raise ValueError(f"chain {chain_id} is not an EVM chain")
        venue = self._venues.get(int(chain_id), {}).get(source)
        if venue is None:
            raise ValueError(f"unsupported venue {source} on chain {chain_id}")
        if int(sell_amount) <= 0:
            raise ValueError("sell_amount must be positive")

        capability_key = (int(chain_id), source, sell_token.lower(), buy_token.lower())
        capability_until = self._pair_capability_cache.get(capability_key, 0.0)
        if capability_until > time.monotonic():
            raise RuntimeError(f"no {source} liquidity for pair (cached capability miss)")
        if capability_until:
            self._pair_capability_cache.pop(capability_key, None)

        if deadline is None:
            deadline = started + float(os.getenv("DEX_MAX_QUOTE_LATENCY_MS", "2500")) / 1000.0

        try:
            if venue.kind == "v2":
                out, path = self._v2_quote(chain_id, venue, sell_token, buy_token, int(sell_amount), deadline=deadline)
                fee_pair = ()
            elif venue.kind == "v3":
                out, path, fee_pair = self._v3_quote(chain_id, venue, sell_token, buy_token, int(sell_amount), deadline=deadline)
            elif venue.kind == "stable":
                out, path, _factory = self._stable_quote(chain_id, venue, sell_token, buy_token, int(sell_amount), deadline=deadline)
                fee_pair = ()
            elif venue.kind == "lb":
                out, path, _amounts = self._lb_quote(chain_id, venue, sell_token, buy_token, int(sell_amount), deadline=deadline)
                fee_pair = ()
            else:
                raise ValueError(f"unsupported venue kind {venue.kind}")
        except RpcRateLimitError as exc:
            # A failed venue quote can be a deterministic pair-capability miss,
            # not an infrastructure outage. Cache that distinction so the
            # scanner does not repeatedly spend RPC calls on dead pairs.
            message = str(exc).lower()
            if "liquidity for pair" in message:
                self._pair_capability_cache[capability_key] = time.monotonic() + self._pair_capability_ttl
                raise RuntimeError(str(exc)) from exc
            raise
        except RuntimeError as exc:
            message = str(exc).lower()
            if "liquidity for pair" in message:
                self._pair_capability_cache[capability_key] = time.monotonic() + self._pair_capability_ttl
            raise

        if out <= 0:
            raise RuntimeError(f"{source} returned zero output")
        latency_ms = Decimal(str((time.perf_counter() - started) * 1000))
        gas_limit = self.gas_limit + 60000 * max(0, len(path) - 2)
        min_out = out * (10_000 - int(slippage_bps)) // 10_000

        if probe:
            execution = DexExecution(
                int(chain_id), venue.name, source, venue.router, "0x", 0, gas_limit, 0,
                sell_token, buy_token, int(sell_amount), out, venue.router,
                {"path": list(path), "fees": list(fee_pair), "probe": True},
            )
            return DexQuote(str(chain_id), venue.name, sell_token, buy_token, Decimal(sell_amount), Decimal(out), Decimal(0), Decimal(0), Decimal("0"), Decimal(slippage_bps), latency_ms), execution

        gas_price = self._gas_price(chain_id, deadline=deadline)
        gas_native = Decimal(gas_limit) * Decimal(gas_price)
        tx_deadline = int(time.time()) + self.deadline_seconds
        data = self._build_calldata(chain_id, venue, sell_token, buy_token, int(sell_amount), int(min_out), taker, path, fee_pair, tx_deadline)
        execution = DexExecution(
            int(chain_id), venue.name, source, venue.router, data, 0, gas_limit, gas_price,
            sell_token, buy_token, int(sell_amount), out, venue.router,
            {"path": list(path), "fees": list(fee_pair), "deadline": tx_deadline},
        )
        return DexQuote(str(chain_id), venue.name, sell_token, buy_token, Decimal(sell_amount), Decimal(out), gas_native, Decimal(0), Decimal("0"), Decimal(slippage_bps), latency_ms), execution

    def _build_calldata(self, chain_id: int, venue: Venue, sell_token: str, buy_token: str, amount_in: int, min_out: int, taker: str, path, fee_pair, tx_deadline: int) -> str:
        w3 = self._pool(chain_id).w3
        if venue.kind == "v2":
            router = self._contract(chain_id, venue.router, V2_ROUTER_ABI)
            return router.encode_abi("swapExactTokensForTokens", args=[amount_in, min_out, [self._addr(t) for t in path], self._addr(taker), tx_deadline])
        if venue.kind == "v3":
            router = self._contract(chain_id, venue.router, V3_ROUTER_ABI)
            if len(path) == 2:
                return router.encode_abi("exactInputSingle", args=[(self._addr(sell_token), self._addr(buy_token), int(fee_pair[0]), self._addr(taker), amount_in, min_out, 0)])
            encoded = self._encode_v3_path(path, fee_pair)
            return router.encode_abi("exactInput", args=[(encoded, self._addr(taker), amount_in, min_out)])
        if venue.kind == "stable":
            router = self._contract(chain_id, venue.router, STABLE_ROUTER_ABI)
            return router.encode_abi("swapExactTokensForTokens", args=[amount_in, min_out, list(path), self._addr(taker), tx_deadline])
        raise ValueError(f"calldata not supported for venue kind {venue.kind}")

def _stable_flag_combinations(hops: int):
    """Yield every stable/volatile flag combination for ``hops`` hops."""
    if hops <= 0:
        return
    for mask in range(2 ** hops):
        yield tuple(bool(mask & (1 << i)) for i in range(hops))
