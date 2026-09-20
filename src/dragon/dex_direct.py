from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal

from web3 import Web3

from .dex import DexQuote
from .dex_0x import DexExecution

BASE_WETH = "0x4200000000000000000000000000000000000006"
MULTICALL3 = "0xca11bde05977b3631167028862be2a173976ca11"
AERO_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
UNI_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNI_SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
AAVE_BASE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"
RPC_CONCURRENCY_SEMAPHORE = __import__("threading").BoundedSemaphore(max(1, int(os.getenv("DEX_RPC_MAX_CONCURRENCY", "4"))))
AAVE_POOL_ABI = [{"inputs":[],"name":"FLASHLOAN_PREMIUM_TOTAL","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"}]

ROUTER_ABI = [
 {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
 {"inputs":[],"name":"defaultFactory","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
]

MULTICALL3_ABI = [{"inputs":[{"components":[{"internalType":"address","name":"target","type":"address"},{"internalType":"bool","name":"allowFailure","type":"bool"},{"internalType":"bytes","name":"callData","type":"bytes"}],"internalType":"struct Multicall3.Call3[]","name":"calls","type":"tuple[]"}],"name":"aggregate3","outputs":[{"components":[{"internalType":"bool","name":"success","type":"bool"},{"internalType":"bytes","name":"returnData","type":"bytes"}],"internalType":"struct Multicall3.Result[]","name":"returnData","type":"tuple[]"}],"stateMutability":"payable","type":"function"}]

UNI_QUOTER_ABI = [
 {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
 {"inputs":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"uint256","name":"amountIn","type":"uint256"}],"name":"quoteExactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160[]","name":"sqrtPriceX96AfterList","type":"uint160[]"},{"internalType":"uint32[]","name":"initializedTicksCrossedList","type":"uint32[]"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
]
UNI_ROUTER_ABI = [
 {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IV3SwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"},
 {"inputs":[{"components":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"}],"internalType":"struct IV3SwapRouter.ExactInputParams","name":"params","type":"tuple"}],"name":"exactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}
]

class RpcRateLimitError(RuntimeError):
    """Raised when all configured RPC attempts are rate-limited/transient."""

class QuoteDeadlineError(RuntimeError):
    """Raised when a quote exceeds its opportunity-latency budget."""

class DirectDexAdapter:
    """Direct Base DEX adapter. No aggregator/API key is required."""

    def __init__(self, rpc_url: str | None = None, timeout: float = 4.0):
        primary = (rpc_url or os.getenv("DEX_RPC_URL") or "").strip()
        fallbacks = [x.strip() for x in os.getenv("DEX_RPC_URLS", "").split(",") if x.strip()]
        urls = []
        for url in [primary, *fallbacks]:
            if url and url not in urls:
                urls.append(url)
        if not urls:
            raise RuntimeError("DEX_RPC_URL is required for direct DEX mode")
        self._rpc_urls = urls
        if len(urls) < 2:
            logging.warning("Only one DEX_RPC endpoint configured; 429 resilience has no failover target")
        # Providers with a paid key tolerate far more traffic than the shared
        # public endpoint, so allow a wider timeout and a cheaper cool-down when
        # a keyed endpoint is present. Detection is intentionally simple: any
        # provider URL contains a path segment after /v2/ or /v3/ other than the
        # public demo key.
        self._keyed_endpoints = [self._is_keyed_rpc_endpoint(url) for url in urls]
        self._has_keyed_endpoint = any(self._keyed_endpoints)
        if self._has_keyed_endpoint:
            logging.info("DEX RPC pool includes a keyed provider endpoint; paid-tier limits enabled")
        configured_timeout = float(os.getenv("DEX_RPC_TIMEOUT_SECONDS", "0.40"))
        # A dedicated provider answers fast, but a slightly larger ceiling keeps
        # healthy paid endpoints from being marked unavailable under bursty
        # concurrent quoting. The shared public RPC keeps the tighter bound.
        timeout_ceiling = 1.0 if self._has_keyed_endpoint else 0.45
        self._rpc_timeout = max(0.15, min(timeout_ceiling, configured_timeout))
        self._rpc_index = 0
        self._rpc_failures = [0 for _ in urls]
        self._rpc_cooldown_until = [0.0 for _ in urls]
        # Per-endpoint success/failure counters make it possible to prove which
        # provider actually served traffic instead of guessing from error text.
        self._rpc_success = [0 for _ in urls]
        self._rpc_errors = [0 for _ in urls]
        self._rpc_selftest = ["untested" for _ in urls]
        # Endpoint selection is shared by concurrent quote workers. Protect the
        # cursor so concurrent requests do not all hammer the same public RPC.
        self._rpc_selection_lock = __import__("threading").Lock()
        self.rpc_url = ""
        self._bound_rpc_cache = {}
        startup_errors = []
        connected = False
        for idx, url in enumerate(urls):
            self._rpc_index = idx
            try:
                self._bind_rpc(url)
                connected = True
                break
            except Exception as exc:
                startup_errors.append(f"endpoint={idx} url={url} error={type(exc).__name__}: {exc}")
                self._rpc_cooldown_until[idx] = time.monotonic() + min(5.0, 0.5 * (idx + 1))
                logging.warning("RPC startup endpoint failed index=%s url=%s error=%s", idx, url, exc)
        if not connected:
            raise RpcRateLimitError("all configured Base RPC endpoints failed at startup: " + " | ".join(startup_errors))
        logging.info(
            "Direct DEX RPC failover configured endpoints=%s active=%s",
            len(urls), self._rpc_host(self.rpc_url),
        )
        try:
            self._rpc_selftest = self._run_rpc_selftest()
        except Exception as exc:
            logging.warning("RPC selftest unavailable error=%s: %s", type(exc).__name__, exc)
            self._rpc_selftest = ["error" for _ in urls]

        self._native_rate_cache = {}
        # Short quote cache reduces duplicate RPC calls during one scan without
        # turning the scanner into a stale-price engine. Live execution can disable it.
        self._quote_cache = {}
        self._quote_cache_ttl = max(0.0, float(os.getenv("DEX_QUOTE_CACHE_SECONDS", "0.25")))
        self._no_liquidity_cache = {}
        self.deadline_seconds = max(5, int(os.getenv("DEX_DEADLINE_SECONDS", "20")))
        self.aero_gas_limit = max(100_000, int(os.getenv("AERODROME_GAS_LIMIT", "250000")))
        self.uni_gas_limit = max(100_000, int(os.getenv("UNISWAP_GAS_LIMIT", "250000")))
        self.uni_fees = tuple(int(x) for x in os.getenv("UNISWAP_V3_FEES", "500,3000,10000").split(",") if x.strip())
        self.multicall_enabled = os.getenv("DEX_MULTICALL_ENABLED", "true").strip().lower() in {"1","true","yes","on"}
        raw_intermediates = os.getenv("DEX_ROUTE_INTERMEDIATES", BASE_WETH).strip()
        self.route_intermediates = tuple(x.strip() for x in raw_intermediates.split(",") if x.strip())
        self.deep_route_always = os.getenv("DEX_DEEP_ROUTE_ALWAYS", "false").strip().lower() in {"1", "true", "yes", "on"}
        self._gas_price_cache = 0
        self._gas_price_cache_at = 0.0
        # Keep route expansion bounded: direct + one-intermediate paths only.
        if len(self.route_intermediates) > 4:
            self.route_intermediates = self.route_intermediates[:4]

    @staticmethod
    def _rpc_host(url: str) -> str:
        """Return a log-safe endpoint label. The API key is never included."""
        if not url:
            return "none"
        try:
            host = url.split("//", 1)[-1].split("/", 1)[0]
        except Exception:
            return "unparseable"
        return host or "unparseable"

    def _run_rpc_selftest(self) -> list[str]:
        """Probe every endpoint with eth_chainId so key/URL problems are explicit.

        A provider that rejects the key returns an HTTP error here, which is the
        only reliable way to distinguish "key invalid" from "endpoint slow".
        This runs in a bounded thread pool: a hung provider socket must never
        block service startup, because Render's health check would then 502 the
        whole service. Every failure is reported, never raised.
        """
        results = ["untested" for _ in self._rpc_urls]
        try:
            with ThreadPoolExecutor(max_workers=min(4, len(self._rpc_urls))) as pool:
                futures = {
                    pool.submit(self._probe_endpoint, url): idx
                    for idx, url in enumerate(self._rpc_urls)
                }
                for future in as_completed(futures, timeout=max(3.0, 2.0 + self._rpc_timeout)):
                    idx = futures[future]
                    try:
                        results[idx] = future.result()
                    except Exception as exc:
                        results[idx] = f"error:{type(exc).__name__}"
                        logging.warning("RPC selftest endpoint=%s host=%s raised %s", idx, self._rpc_host(self._rpc_urls[idx]), type(exc).__name__)
        except Exception as exc:
            logging.warning("RPC selftest aborted error=%s: %s", type(exc).__name__, exc)
        return results

    def _probe_endpoint(self, url: str) -> str:
        idx = self._rpc_urls.index(url)
        label = self._rpc_host(url)
        expected = "0x2105"
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": max(2.0, self._rpc_timeout)}))
            chain = hex(int(w3.eth.chain_id))
            if chain == expected:
                logging.info("RPC selftest endpoint=%s host=%s chain=%s keyed=%s", idx, label, chain, self._keyed_endpoints[idx])
                return "ok"
            logging.warning("RPC selftest endpoint=%s host=%s returned chain=%s; Base expected %s", idx, label, chain, expected)
            return f"wrong_chain:{chain}"
        except Exception as exc:
            message = str(exc)
            if "401" in message or "403" in message or "unauthorized" in message.lower() or "invalid" in message.lower():
                logging.warning("RPC selftest endpoint=%s host=%s auth_failed keyed=%s", idx, label, self._keyed_endpoints[idx])
                return "auth_failed"
            logging.warning("RPC selftest endpoint=%s host=%s failed keyed=%s error=%s: %s", idx, label, self._keyed_endpoints[idx], type(exc).__name__, message[:200])
            return f"error:{type(exc).__name__}"

    def rpc_status(self) -> dict:
        """Public, secret-free view of the RPC pool for the health endpoint."""
        return {
            "active_host": self._rpc_host(self.rpc_url),
            "endpoints": [
                {
                    "host": self._rpc_host(url),
                    "keyed": bool(keyed),
                    "selftest": self._rpc_selftest[idx] if idx < len(self._rpc_selftest) else "unknown",
                    "success": self._rpc_success[idx],
                    "errors": self._rpc_errors[idx],
                    "cooling_down": self._rpc_cooldown_until[idx] > time.monotonic(),
                }
                for idx, (url, keyed) in enumerate(zip(self._rpc_urls, self._keyed_endpoints))
            ],
        }

    def clone_for_concurrent_quotes(self):
        """Create an isolated adapter so parallel quote calls do not share RPC failover state."""
        return DirectDexAdapter(rpc_url=self.rpc_url, timeout=self._rpc_timeout)

    @staticmethod
    def _addr(v: str) -> str:
        return Web3.to_checksum_address(v)

    def _bind_rpc(self, url: str) -> None:
        cached = self._bound_rpc_cache.get(url)
        if cached is not None:
            self.rpc_url, self.w3, self.aero, self.uni_quoter, self.uni_router, self.aave_pool, self.aero_factory, self.multicall3 = cached
            return
        self.rpc_url = url
        self.w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": self._rpc_timeout}))
        if not self.w3.is_connected():
            raise RuntimeError(f"cannot connect to Base RPC endpoint: {url}")
        if self.w3.eth.chain_id != 8453:
            raise RuntimeError(f"direct DEX adapter requires Base chain 8453, got {self.w3.eth.chain_id}")
        self.aero = self.w3.eth.contract(address=Web3.to_checksum_address(AERO_ROUTER), abi=ROUTER_ABI)
        self.uni_quoter = self.w3.eth.contract(address=Web3.to_checksum_address(UNI_QUOTER_V2), abi=UNI_QUOTER_ABI)
        self.uni_router = self.w3.eth.contract(address=Web3.to_checksum_address(UNI_SWAP_ROUTER), abi=UNI_ROUTER_ABI)
        self.aave_pool = self.w3.eth.contract(address=Web3.to_checksum_address(os.getenv("DEX_AAVE_POOL_ADDRESS", AAVE_BASE_POOL)), abi=AAVE_POOL_ABI)
        self.aero_factory = self._addr(self.aero.functions.defaultFactory().call())
        self.multicall3 = self.w3.eth.contract(address=self._addr(MULTICALL3), abi=MULTICALL3_ABI)
        self._bound_rpc_cache[url] = (self.rpc_url, self.w3, self.aero, self.uni_quoter, self.uni_router, self.aave_pool, self.aero_factory, self.multicall3)

    @staticmethod
    def _is_keyed_rpc_endpoint(url: str) -> bool:
        """Detect a paid/keyed provider URL, ignoring public and demo keys."""
        if not re.search(r"/(v2|v3)/[A-Za-z0-9_-]{8,}", url):
            return False
        tail = url.rstrip("/").rsplit("/", 1)[-1].lower()
        return tail not in {"docs-demo", "demo", "your-key", "your_key", "yourkey"}

    @staticmethod
    def _is_transient_rpc_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(x in msg for x in ("429", "too many requests", "rate limit", "rate limit exceeded", "usage limit", "reached the usage limit", "-32001", "gateway timeout", "timed out", "read timeout", "timeout", "temporarily unavailable", "503 service unavailable", "service unavailable", "connection reset", "connection aborted"))

    @staticmethod
    def _only_rpc_failures(errors: list[str]) -> bool:
        return bool(errors) and all(
            "RpcRateLimitError" in error
            or "429" in error
            or "rate limit" in error.lower()
            or "deadline exhausted" in error.lower()
            for error in errors
        )

    def _rpc_call(self, fn, deadline: float | None = None):
        # Fail fast across the endpoint pool. Never spend several seconds
        # retrying rate-limited public RPCs inside a single quote, because that
        # turns a transient 429 into a false 6-10s quote-latency failure.
        last_exc = None
        attempted = set()
        max_attempts = max(1, min(len(self._rpc_urls), int(os.getenv("DEX_RPC_MAX_ATTEMPTS", "2"))))
        for _ in range(max_attempts):
            now = time.monotonic()
            ready = [i for i in range(len(self._rpc_urls)) if i not in attempted and self._rpc_cooldown_until[i] <= now]
            if not ready:
                remaining = [i for i in range(len(self._rpc_urls)) if i not in attempted]
                if not remaining:
                    break
                # Do not sleep for the full provider cooldown inside a quote.
                idx = min(remaining, key=lambda i: self._rpc_cooldown_until[i])
            else:
                # Prefer keyed provider endpoints over the shared public RPC.
                # The public endpoint is a last-resort fallback, so a leftover
                # public URL in the pool must never lead the rotation.
                keyed_ready = [i for i in ready if self._keyed_endpoints[i]]
                candidates = keyed_ready or ready
                # Round-robin across ready endpoints instead of pinning every
                # successful request to the current endpoint. This is critical
                # for shared/public RPC pools where one endpoint can return 429
                # while another remains healthy.
                with self._rpc_selection_lock:
                    ordered = candidates
                    idx = next((i for i in ordered if i >= self._rpc_index), ordered[0])
                    self._rpc_index = (idx + 1) % len(self._rpc_urls)
            if deadline is not None and time.monotonic() >= deadline:
                break
            attempted.add(idx)
            self._rpc_index = idx
            if self.rpc_url != self._rpc_urls[idx]:
                try:
                    self._bind_rpc(self._rpc_urls[idx])
                except Exception as exc:
                    last_exc = exc
                    self._rpc_cooldown_until[idx] = time.monotonic() + 10.0
                    continue
            try:
                if deadline is not None and time.monotonic() + self._rpc_timeout > deadline:
                    raise QuoteDeadlineError("RPC call skipped: quote deadline exhausted")
                with RPC_CONCURRENCY_SEMAPHORE:
                    result = fn(self.w3)
                self._rpc_failures[idx] = 0
                self._rpc_success[idx] += 1
                return result
            except QuoteDeadlineError:
                # A latency-budget expiry is not an RPC rate-limit/liquidity failure.
                # Preserve the reason so callers can classify it as QUOTE_DEADLINE.
                raise
            except Exception as exc:
                last_exc = exc
                if not self._is_transient_rpc_error(exc):
                    self._rpc_errors[idx] += 1
                    raise
                self._rpc_failures[idx] += 1
                self._rpc_errors[idx] += 1
                # Public endpoints need a meaningful cool-down. Hammering a
                # 429 endpoint every 250ms only extends the outage. A keyed
                # provider recovers quickly, so it should not be parked for
                # 30s after one transient blip.
                base_backoff = 1.5 if self._keyed_endpoints[idx] else 5.0
                cap = 10.0 if self._keyed_endpoints[idx] else 30.0
                backoff = min(cap, base_backoff * (2 ** min(self._rpc_failures[idx] - 1, 2)))
                self._rpc_cooldown_until[idx] = time.monotonic() + backoff
                logging.warning("RPC transient error endpoint=%s keyed=%s cooldown=%.2fs error=%s", idx, self._keyed_endpoints[idx], backoff, exc)
                self._rpc_index = (idx + 1) % len(self._rpc_urls)
        all_cooling = all(self._rpc_cooldown_until[i] > time.monotonic() for i in range(len(self._rpc_urls)))
        if all_cooling:
            # Every endpoint is in cool-down, so no request was even attempted.
            # Saying "unavailable after 0 fast attempts" hid the real cause.
            reason = "all Base RPC endpoints are in cool-down; quote deadline too short to wait"
        elif not attempted:
            reason = "no RPC endpoint was eligible within the quote deadline"
        else:
            reason = f"all Base RPC attempts failed: {last_exc}"
        raise RpcRateLimitError(reason)

    def _multicall(self, calls, deadline: float | None = None):
        if not self.multicall_enabled: raise RuntimeError("Multicall3 disabled")
        payload=[(self._addr(t),bool(a),d) for t,a,d in calls]
        return self._rpc_call(lambda w3: self.multicall3.functions.aggregate3(payload).call(), deadline=deadline)

    def _uni_calldata(self, token_in, token_out, amount, fee):
        return self.uni_quoter.encode_abi("quoteExactInputSingle", args=[(self._addr(token_in),self._addr(token_out),int(amount),int(fee),0)])

    def sources(self, chain_id: int) -> tuple[str, ...]:
        if int(chain_id) != 8453:
            return ()
        return ("Uniswap_V3", "Aerodrome")

    def native_to_quote_rate(self, *, chain_id: int, quote_token: str, sell_amount_native: int, taker: str) -> Decimal:
        key=(int(chain_id), quote_token.lower())
        cached=self._native_rate_cache.get(key)
        now=time.monotonic()
        if cached and now-cached[0] < 15.0:
            return cached[1]
        q, _ = self.quote_single_source(chain_id=chain_id, sell_token=BASE_WETH, buy_token=quote_token, sell_amount=sell_amount_native, taker=taker, source="Uniswap_V3", slippage_bps=50)
        rate=q.buy_amount / q.sell_amount
        self._native_rate_cache[key]=(now,rate)
        return rate

    def flash_loan_fee_bps(self) -> Decimal:
        raw=Decimal(str(self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(os.getenv("DEX_AAVE_POOL_ADDRESS", AAVE_BASE_POOL)), abi=AAVE_POOL_ABI).functions.FLASHLOAN_PREMIUM_TOTAL().call())))
        if raw < 0 or raw > Decimal("1000"):
            raise RuntimeError(f"invalid Aave flash-loan premium: {raw} bps")
        return raw

    def _encode_uni_path(self, tokens, fees):
        data = bytearray()
        for i, token in enumerate(tokens):
            data.extend(bytes.fromhex(self._addr(token)[2:]))
            if i < len(fees): data.extend(int(fees[i]).to_bytes(3, "big"))
        return "0x" + data.hex()

    def _uni_quote(self, token_in: str, token_out: str, amount: int, deadline: float | None = None):
        cache_key=("uni",token_in.lower(),token_out.lower(),int(amount)); now=time.monotonic()
        cached=self._quote_cache.get(cache_key)
        if cached and now-cached[0] < self._quote_cache_ttl: return cached[1]
        best=None; errors=[]; paths=[(token_in,token_out)]
        for mid in self.route_intermediates:
            if mid.lower() not in {token_in.lower(),token_out.lower()}: paths.append((token_in,mid,token_out))
        for path_index,path in enumerate(paths):
            if deadline is not None and time.monotonic() >= deadline:
                raise QuoteDeadlineError("Uniswap V3 quote deadline exhausted before route probe")
            if path_index>0 and best is not None and not self.deep_route_always: break
            fee_sets=[(f,) for f in self.uni_fees] if len(path)==2 else [(a,b) for a in self.uni_fees for b in self.uni_fees]
            if len(path)==2 and self.multicall_enabled:
                try:
                    results=self._multicall([(UNI_QUOTER_V2,True,self._uni_calldata(token_in,token_out,amount,f[0])) for f in fee_sets], deadline=deadline)
                    for fees,(success,data) in zip(fee_sets,results):
                        if not success: continue
                        d=self.w3.codec.decode(["uint256","uint160","uint32","uint256"],bytes(data)); out=int(d[0]); gas_est=int(d[3])
                        if out>0 and (best is None or out>best[0]): best=(out,tuple(path),tuple(fees),gas_est,self._encode_uni_path(path,fees))
                except QuoteDeadlineError:
                    raise
                except Exception as exc: errors.append(f"multicall: {type(exc).__name__}: {exc}")
            if best is None:
                def quote_fee(fees):
                    encoded=self._encode_uni_path(path,fees)
                    if len(path)==2: result=self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(UNI_QUOTER_V2),abi=UNI_QUOTER_ABI).functions.quoteExactInputSingle((self._addr(token_in),self._addr(token_out),int(amount),int(fees[0]),0)).call(), deadline=deadline)
                    else: result=self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(UNI_QUOTER_V2),abi=UNI_QUOTER_ABI).functions.quoteExactInput(encoded,int(amount)).call(), deadline=deadline)
                    return fees,encoded,result
                with ThreadPoolExecutor(max_workers=min(4,len(fee_sets))) as pool:
                    futures=[pool.submit(quote_fee,fees) for fees in fee_sets]
                    for future in as_completed(futures):
                        try:
                            fees,encoded,result=future.result(); out=int(result[0]); gas_est=int(result[-1])
                            if out>0 and (best is None or out>best[0]): best=(out,tuple(path),tuple(fees),gas_est,encoded)
                        except QuoteDeadlineError:
                            raise
                        except Exception as exc: errors.append(f"rpc fee_probe: {type(exc).__name__}: {exc}")
        if best is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise QuoteDeadlineError("Uniswap V3 quote deadline exhausted; pair liquidity was not confirmed")
            message="no Uniswap V3 pool/liquidity for pair; "+" | ".join(errors[-4:])
            if self._only_rpc_failures(errors):
                raise RpcRateLimitError(message)
            raise RuntimeError(message)
        self._quote_cache[cache_key]=(now,best)
        logging.info("Uniswap V3 quote pair=%s->%s amount=%s out=%s fees=%s multicall=%s",token_in,token_out,amount,best[0],best[2],self.multicall_enabled)
        return best
    def _aero_quote(self, token_in: str, token_out: str, amount: int, deadline: float | None = None):
        cache_key=("aero",token_in.lower(),token_out.lower(),int(amount)); now=time.monotonic()
        cached=self._quote_cache.get(cache_key)
        if cached and now-cached[0] < self._quote_cache_ttl: return cached[1]
        factory=self.aero_factory; best=None; errors=[]; paths=[(token_in,token_out)]
        for mid in self.route_intermediates:
            if mid.lower() not in {token_in.lower(),token_out.lower()}: paths.append((token_in,mid,token_out))
        for path_index,path in enumerate(paths):
            if path_index>0 and best is not None and not self.deep_route_always: break
            stable_sets=[(False,),(True,)] if len(path)==2 else [(False,False),(False,True),(True,False),(True,True)]
            if self.multicall_enabled:
                try:
                    routes=[]; calls=[]
                    for flags in stable_sets:
                        if deadline is not None and time.monotonic() >= deadline:
                            raise QuoteDeadlineError("Aerodrome quote deadline exhausted during route construction")
                        route=[{"from":self._addr(path[i]),"to":self._addr(path[i+1]),"stable":bool(flags[i]),"factory":self._addr(factory)} for i in range(len(path)-1)]
                        routes.append(route); calls.append((AERO_ROUTER,True,self.aero.encode_abi("getAmountsOut",args=[int(amount),route])))
                    results=self._multicall(calls, deadline=deadline)
                    for route,(success,data) in zip(routes,results):
                        if not success: continue
                        out=int(self.w3.codec.decode(["uint256[]"],bytes(data))[0][-1])
                        if out>0 and (best is None or out>best[0]): best=(out,tuple(route),self.aero_gas_limit+60000*(len(path)-1),self._addr(factory))
                except QuoteDeadlineError:
                    raise
                except Exception as exc: errors.append(f"multicall: {type(exc).__name__}: {exc}")
            if best is None:
                def probe(flags):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise QuoteDeadlineError("Aerodrome quote deadline exhausted before RPC probe")
                    route=[{"from":self._addr(path[i]),"to":self._addr(path[i+1]),"stable":bool(flags[i]),"factory":self._addr(factory)} for i in range(len(path)-1)]
                    amounts=self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(AERO_ROUTER),abi=ROUTER_ABI).functions.getAmountsOut(int(amount),route).call(), deadline=deadline)
                    return route,int(amounts[-1])
                with ThreadPoolExecutor(max_workers=min(4,len(stable_sets))) as pool:
                    futures=[pool.submit(probe,s) for s in stable_sets]
                    for future in as_completed(futures):
                        try:
                            route,out=future.result()
                            if out>0 and (best is None or out>best[0]): best=(out,tuple(route),self.aero_gas_limit+60000*(len(path)-1),self._addr(factory))
                        except QuoteDeadlineError:
                            raise
                        except Exception as exc: errors.append(f"rpc stable_probe: {type(exc).__name__}: {exc}")
        if best is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise QuoteDeadlineError("Aerodrome quote deadline exhausted; pair liquidity was not confirmed")
            message="no Aerodrome pool/liquidity for pair; "+" | ".join(errors[-4:])
            if self._only_rpc_failures(errors):
                raise RpcRateLimitError(message)
            raise RuntimeError(message)
        self._quote_cache[cache_key]=(now,best)
        logging.info("Aerodrome quote pair=%s->%s amount=%s out=%s multicall=%s",token_in,token_out,amount,best[0],self.multicall_enabled)
        return best
    def _gas_price(self, deadline: float | None = None) -> int:
        now = time.monotonic()
        if self._gas_price_cache <= 0 or now - self._gas_price_cache_at >= 1.0:
            self._gas_price_cache = int(self._rpc_call(lambda w3: w3.eth.gas_price, deadline=deadline))
            self._gas_price_cache_at = now
        return self._gas_price_cache

    def quote_single_source(self, *, chain_id: int, sell_token: str, buy_token: str, sell_amount: int, taker: str, source: str, slippage_bps: int = 50, deadline: float | None = None, probe: bool = False):
        started = time.perf_counter()
        if deadline is None:
            deadline = started + float(os.getenv("DEX_MAX_QUOTE_LATENCY_MS", "500")) / 1000.0
        if int(chain_id) != 8453:
            raise ValueError("direct adapter supports Base only")
        if int(sell_amount) <= 0:
            raise ValueError("sell_amount must be positive")
        if source == "Uniswap_V3":
            quote_started = time.perf_counter()
            out, path, fees, gas_limit, encoded_path = self._uni_quote(sell_token, buy_token, int(sell_amount), deadline=deadline)
            quote_latency_ms = (time.perf_counter() - quote_started) * 1000
            if probe:
                execution = DexExecution(8453, "Uniswap_V3", "Uniswap_V3", UNI_SWAP_ROUTER, "0x", 0, gas_limit, 0, sell_token, buy_token, int(sell_amount), out, UNI_SWAP_ROUTER, {"path": path, "fees": fees, "probe": True})
                return DexQuote("8453", "Uniswap_V3", sell_token, buy_token, Decimal(sell_amount), Decimal(out), Decimal(0), Decimal(0), Decimal("0"), Decimal(slippage_bps), Decimal(str((time.perf_counter() - started) * 1000))), execution
            gas_price = self._gas_price(deadline=deadline)
            gas_native = Decimal(gas_limit) * Decimal(gas_price)
            min_out = out * (10_000 - int(slippage_bps)) // 10_000
            tx_deadline = int(time.time()) + self.deadline_seconds
            tx = (self.uni_router.functions.exactInputSingle((self._addr(sell_token), self._addr(buy_token), int(fees[0]), self._addr(taker), int(sell_amount), int(min_out), 0)) if len(path) == 2 else self.uni_router.functions.exactInput((encoded_path, self._addr(taker), int(sell_amount), int(min_out)))).build_transaction({"from": self._addr(taker), "value": 0, "gas": gas_limit, "gasPrice": gas_price})
            execution = DexExecution(8453, "Uniswap_V3", "Uniswap_V3", UNI_SWAP_ROUTER, tx["data"], 0, gas_limit, gas_price, sell_token, buy_token, int(sell_amount), out, UNI_SWAP_ROUTER, {"path": path, "fees": fees, "deadline": tx_deadline})
            return DexQuote("8453", "Uniswap_V3", sell_token, buy_token, Decimal(sell_amount), Decimal(out), gas_native, Decimal(0), Decimal("0"), Decimal(slippage_bps), Decimal(str((time.perf_counter() - started) * 1000))), execution
        if source == "Aerodrome":
            quote_started = time.perf_counter()
            out, route, gas_limit, factory = self._aero_quote(sell_token, buy_token, int(sell_amount), deadline=deadline)
            quote_latency_ms = (time.perf_counter() - quote_started) * 1000
            if probe:
                execution = DexExecution(8453, "Aerodrome", "Aerodrome", AERO_ROUTER, "0x", 0, gas_limit, 0, sell_token, buy_token, int(sell_amount), out, AERO_ROUTER, {"route": route, "hops": len(route), "factory": factory, "probe": True})
                return DexQuote("8453", "Aerodrome", sell_token, buy_token, Decimal(sell_amount), Decimal(out), Decimal(0), Decimal(0), Decimal("0"), Decimal(slippage_bps), Decimal(str((time.perf_counter() - started) * 1000))), execution
            gas_price = self._gas_price()
            gas_native = Decimal(gas_limit) * Decimal(gas_price)
            min_out = out * (10_000 - int(slippage_bps)) // 10_000
            tx_deadline = int(time.time()) + self.deadline_seconds
            tx = self.aero.functions.swapExactTokensForTokens(int(sell_amount), int(min_out), route, self._addr(taker), tx_deadline).build_transaction({"from": self._addr(taker), "value": 0, "gas": gas_limit, "gasPrice": gas_price})
            execution = DexExecution(8453, "Aerodrome", "Aerodrome", AERO_ROUTER, tx["data"], 0, gas_limit, gas_price, sell_token, buy_token, int(sell_amount), out, AERO_ROUTER, {"route": route, "hops": len(route), "factory": factory, "deadline": deadline})
            return DexQuote("8453", "Aerodrome", sell_token, buy_token, Decimal(sell_amount), Decimal(out), gas_native, Decimal(0), Decimal("0"), Decimal(slippage_bps), Decimal(str((time.perf_counter() - quote_started) * 1000))), execution
        raise ValueError(f"unsupported direct DEX source: {source}")

    def close(self) -> None:
        w3 = getattr(self, "w3", None)
        provider = getattr(w3, "provider", None)
        if provider and hasattr(provider, "disconnect"):
            provider.disconnect()
