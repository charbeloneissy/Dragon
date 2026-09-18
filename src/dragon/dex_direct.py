from __future__ import annotations

import logging
import os
import time
from decimal import Decimal

from web3 import Web3

from .dex import DexQuote
from .dex_0x import DexExecution

BASE_WETH = "0x4200000000000000000000000000000000000006"
AERO_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"
UNI_QUOTER_V2 = "0x3d4e44Eb1374240CE5F1B871ab261CD16335B76a"
UNI_SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
AAVE_BASE_POOL = "0xa238dd80c259a72e81d7e4664a9801593f98d1c5"
AAVE_POOL_ABI = [{"inputs":[],"name":"FLASHLOAN_PREMIUM_TOTAL","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"}]

ROUTER_ABI = [
 {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMin","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"},{"internalType":"address","name":"to","type":"address"},{"internalType":"uint256","name":"deadline","type":"uint256"}],"name":"swapExactTokensForTokens","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"nonpayable","type":"function"},
 {"inputs":[],"name":"defaultFactory","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"internalType":"uint256","name":"amountIn","type":"uint256"},{"components":[{"internalType":"address","name":"from","type":"address"},{"internalType":"address","name":"to","type":"address"},{"internalType":"bool","name":"stable","type":"bool"},{"internalType":"address","name":"factory","type":"address"}],"internalType":"struct IRouter.Route[]","name":"routes","type":"tuple[]"}],"name":"getAmountsOut","outputs":[{"internalType":"uint256[]","name":"amounts","type":"uint256[]"}],"stateMutability":"view","type":"function"},
]

UNI_QUOTER_ABI = [
 {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IQuoterV2.QuoteExactInputSingleParams","name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceX96After","type":"uint160"},{"internalType":"uint32","name":"initializedTicksCrossed","type":"uint32"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
 {"inputs":[{"internalType":"bytes","name":"path","type":"bytes"},{"internalType":"uint256","name":"amountIn","type":"uint256"}],"name":"quoteExactInput","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"},{"internalType":"uint160[]","name":"sqrtPriceX96AfterList","type":"uint160[]"},{"internalType":"uint32[]","name":"initializedTicksCrossedList","type":"uint32[]"},{"internalType":"uint256","name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},
]
UNI_ROUTER_ABI = [
 {"inputs":[{"components":[{"internalType":"address","name":"tokenIn","type":"address"},{"internalType":"address","name":"tokenOut","type":"address"},{"internalType":"uint24","name":"fee","type":"uint24"},{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amountIn","type":"uint256"},{"internalType":"uint256","name":"amountOutMinimum","type":"uint256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"}],"internalType":"struct IV3SwapRouter.ExactInputSingleParams","name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"internalType":"uint256","name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}
]

class RpcRateLimitError(RuntimeError):
    """Raised when all configured RPC attempts are rate-limited/transient."""

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
        self._rpc_timeout = float(timeout)
        self._rpc_index = 0
        self._rpc_failures = [0 for _ in urls]
        self._rpc_cooldown_until = [0.0 for _ in urls]
        self.rpc_url = urls[0]
        self._bind_rpc(self.rpc_url)
        logging.info("Direct DEX RPC failover configured endpoints=%s", len(urls))

        self._native_rate_cache = {}
        # Short quote cache reduces duplicate RPC calls during one scan without
        # turning the scanner into a stale-price engine. Live execution can disable it.
        self._quote_cache = {}
        self._quote_cache_ttl = max(0.0, float(os.getenv("DEX_QUOTE_CACHE_SECONDS", "0.25")))
        self._no_liquidity_cache = {}
        self.deadline_seconds = max(5, int(os.getenv("DEX_DEADLINE_SECONDS", "20")))
        self.aero_gas_limit = max(100_000, int(os.getenv("AERODROME_GAS_LIMIT", "250000")))
        self.uni_gas_limit = max(100_000, int(os.getenv("UNISWAP_GAS_LIMIT", "250000")))
        self.uni_fees = tuple(int(x) for x in os.getenv("UNISWAP_V3_FEES", "100,500,3000,10000").split(",") if x.strip())
        raw_intermediates = os.getenv("DEX_ROUTE_INTERMEDIATES", "").strip()
        self.route_intermediates = tuple(x.strip() for x in raw_intermediates.split(",") if x.strip())
        self.deep_route_always = os.getenv("DEX_DEEP_ROUTE_ALWAYS", "false").strip().lower() in {"1", "true", "yes", "on"}
        self._gas_price_cache = 0
        self._gas_price_cache_at = 0.0
        # Keep route expansion bounded: direct + one-intermediate paths only.
        if len(self.route_intermediates) > 4:
            self.route_intermediates = self.route_intermediates[:4]

    def clone_for_concurrent_quotes(self):
        """Create an isolated adapter so parallel quote calls do not share RPC failover state."""
        return DirectDexAdapter(rpc_url=self.rpc_url, timeout=self._rpc_timeout)

    @staticmethod
    def _addr(v: str) -> str:
        return Web3.to_checksum_address(v)

    def _bind_rpc(self, url: str) -> None:
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

    @staticmethod
    def _is_transient_rpc_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(x in msg for x in ("429", "too many requests", "rate limit", "gateway timeout", "temporarily unavailable", "503 service unavailable"))

    def _rpc_call(self, fn):
        last_exc = None
        for _ in range(len(self._rpc_urls)):
            now = time.monotonic()
            ready = [i for i in range(len(self._rpc_urls)) if self._rpc_cooldown_until[i] <= now]
            if not ready:
                time.sleep(min(0.25, max(0.0, min(self._rpc_cooldown_until) - now)))
                ready = [min(range(len(self._rpc_urls)), key=lambda i: self._rpc_cooldown_until[i])]
            idx = self._rpc_index if self._rpc_index in ready else ready[0]
            self._rpc_index = idx
            if self.rpc_url != self._rpc_urls[idx]:
                try:
                    self._bind_rpc(self._rpc_urls[idx])
                except Exception as exc:
                    last_exc = exc
                    self._rpc_cooldown_until[idx] = time.monotonic() + 0.5
                    continue
            try:
                result = fn(self.w3)
                self._rpc_failures[idx] = 0
                return result
            except Exception as exc:
                last_exc = exc
                if not self._is_transient_rpc_error(exc):
                    raise
                self._rpc_failures[idx] += 1
                backoff = min(2.0, 0.25 * (2 ** min(self._rpc_failures[idx] - 1, 3)))
                self._rpc_cooldown_until[idx] = time.monotonic() + backoff
                logging.warning("RPC transient error endpoint=%s backoff=%.2fs error=%s", idx, backoff, exc)
                self._rpc_index = (idx + 1) % len(self._rpc_urls)
        raise RpcRateLimitError(f"all configured Base RPC endpoints failed after {len(self._rpc_urls)} attempts: {last_exc}")

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

    def _uni_quote(self, token_in: str, token_out: str, amount: int):
        cache_key=("uni", token_in.lower(), token_out.lower(), int(amount))
        now=time.monotonic()
        cached=self._quote_cache.get(cache_key)
        if cached and now-cached[0] < self._quote_cache_ttl:
            return cached[1]
        no_liq=self._no_liquidity_cache.get(cache_key)
        if no_liq and now-no_liq < 5.0:
            raise RuntimeError("no Uniswap V3 pool/liquidity for pair (cached)")
        best = None; errors = []
        paths = [(token_in, token_out)]
        for mid in self.route_intermediates:
            if mid.lower() not in {token_in.lower(), token_out.lower()}: paths.append((token_in, mid, token_out))
        for path_index, path in enumerate(paths):
            # Direct pools are the cheap first-stage screen. Only expand multi-hop
            # routes when no direct route exists unless explicitly requested.
            if path_index > 0 and best is not None and not self.deep_route_always:
                break
            fee_sets = [(f,) for f in self.uni_fees] if len(path) == 2 else [(a,b) for a in self.uni_fees for b in self.uni_fees]
            for fees in fee_sets:
                try:
                    encoded = self._encode_uni_path(path, fees)
                    if len(path) == 2:
                        result = self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(UNI_QUOTER_V2), abi=UNI_QUOTER_ABI).functions.quoteExactInputSingle((self._addr(token_in), self._addr(token_out), int(amount), int(fees[0]), 0)).call())
                    else:
                        result = self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(UNI_QUOTER_V2), abi=UNI_QUOTER_ABI).functions.quoteExactInput(encoded, int(amount)).call())
                    out = int(result[0]); gas_est = int(result[-1])
                    if out > 0 and (best is None or out > best[0]): best = (out, tuple(path), tuple(fees), gas_est, encoded)
                except Exception as exc: errors.append(f"path={len(path)} fees={fees}: {type(exc).__name__}: {exc}")
        if best is None:
            detail = " | ".join(errors[-4:])
            logging.warning("Uniswap V3 quote failed pair=%s->%s amount=%s errors=%s", token_in, token_out, amount, detail)
            self._no_liquidity_cache[cache_key]=now
            raise RuntimeError(f"no Uniswap V3 pool/liquidity for pair; {detail}")
        self._quote_cache[cache_key]=(now,best)
        logging.info("Uniswap V3 quote pair=%s->%s amount=%s out=%s hops=%s fees=%s gas=%s", token_in, token_out, amount, best[0], len(best[1])-1, best[2], best[3])
        return best
    def _aero_quote(self, token_in: str, token_out: str, amount: int):
        cache_key=("aero", token_in.lower(), token_out.lower(), int(amount))
        now=time.monotonic()
        cached=self._quote_cache.get(cache_key)
        if cached and now-cached[0] < self._quote_cache_ttl:
            return cached[1]
        no_liq=self._no_liquidity_cache.get(cache_key)
        if no_liq and now-no_liq < 5.0:
            raise RuntimeError("no Aerodrome pool/liquidity for pair (cached)")
        factory = self.aero_factory
        best = None
        errors = []
        paths = [(token_in, token_out)]
        for mid in self.route_intermediates:
            if mid.lower() not in {token_in.lower(), token_out.lower()}:
                paths.append((token_in, mid, token_out))
        for path_index, path in enumerate(paths):
            if path_index > 0 and best is not None and not self.deep_route_always:
                break
            for stable_flags in ([(False,)] if len(path) == 2 else [(False, False), (False, True), (True, False), (True, True)]):
                route = [{"from": self._addr(path[i]), "to": self._addr(path[i+1]), "stable": bool(stable_flags[i]), "factory": self._addr(factory)} for i in range(len(path)-1)]
                try:
                    amounts = self._rpc_call(lambda w3: w3.eth.contract(address=Web3.to_checksum_address(AERO_ROUTER), abi=ROUTER_ABI).functions.getAmountsOut(int(amount), route).call())
                    out = int(amounts[-1])
                    if out > 0 and (best is None or out > best[0]):
                        best = (out, tuple(route), self.aero_gas_limit + 60000 * (len(path)-1), self._addr(factory))
                except Exception as exc:
                    errors.append(f"path={len(path)} stable={stable_flags}: {type(exc).__name__}: {exc}")
        if best is None:
            detail = " | ".join(errors[-2:])
            logging.warning("Aerodrome quote failed pair=%s->%s amount=%s errors=%s", token_in, token_out, amount, detail)
            self._no_liquidity_cache[cache_key]=now
            raise RuntimeError(f"no Aerodrome pool/liquidity for pair; {detail}")
        self._quote_cache[cache_key]=(now,best)
        logging.info("Aerodrome quote pair=%s->%s amount=%s out=%s hops=%s factory=%s", token_in, token_out, amount, best[0], len(best[1]), best[3])
        return best

    def _gas_price(self) -> int:
        now = time.monotonic()
        if self._gas_price_cache <= 0 or now - self._gas_price_cache_at >= 1.0:
            self._gas_price_cache = int(self._rpc_call(lambda w3: w3.eth.gas_price))
            self._gas_price_cache_at = now
        return self._gas_price_cache

    def quote_single_source(self, *, chain_id: int, sell_token: str, buy_token: str, sell_amount: int, taker: str, source: str, slippage_bps: int = 50):
        started = time.perf_counter()
        if int(chain_id) != 8453:
            raise ValueError("direct adapter supports Base only")
        if int(sell_amount) <= 0:
            raise ValueError("sell_amount must be positive")
        if source == "Uniswap_V3":
            quote_started = time.perf_counter()
            out, path, fees, gas_limit, encoded_path = self._uni_quote(sell_token, buy_token, int(sell_amount))
            gas_native = Decimal(gas_limit) * Decimal(self._gas_price())
            min_out = out * (10_000 - int(slippage_bps)) // 10_000
            deadline = int(time.time()) + self.deadline_seconds
            tx = (self.uni_router.functions.exactInputSingle((self._addr(sell_token), self._addr(buy_token), int(fees[0]), self._addr(taker), int(sell_amount), int(min_out), 0)) if len(path) == 2 else self.uni_router.functions.exactInput((encoded_path, self._addr(taker), int(sell_amount), int(min_out)))).build_transaction({"from": self._addr(taker), "value": 0, "gas": gas_limit, "gasPrice": self._gas_price()})
            execution = DexExecution(8453, "Uniswap_V3", "Uniswap_V3", UNI_SWAP_ROUTER, tx["data"], 0, gas_limit, self._gas_price(), sell_token, buy_token, int(sell_amount), out, UNI_SWAP_ROUTER, {"path": path, "fees": fees, "deadline": deadline})
            return DexQuote("8453", "Uniswap_V3", sell_token, buy_token, Decimal(sell_amount), Decimal(out), gas_native, Decimal(0), Decimal("0"), Decimal(slippage_bps), Decimal(str((time.perf_counter() - quote_started) * 1000))), execution
        if source == "Aerodrome":
            quote_started = time.perf_counter()
            out, route, gas_limit, factory = self._aero_quote(sell_token, buy_token, int(sell_amount))
            gas_native = Decimal(gas_limit) * Decimal(self._gas_price())
            min_out = out * (10_000 - int(slippage_bps)) // 10_000
            deadline = int(time.time()) + self.deadline_seconds
            tx = self.aero.functions.swapExactTokensForTokens(int(sell_amount), int(min_out), route, self._addr(taker), deadline).build_transaction({"from": self._addr(taker), "value": 0, "gas": gas_limit, "gasPrice": self._gas_price()})
            execution = DexExecution(8453, "Aerodrome", "Aerodrome", AERO_ROUTER, tx["data"], 0, gas_limit, self._gas_price(), sell_token, buy_token, int(sell_amount), out, AERO_ROUTER, {"route": route, "hops": len(route), "factory": factory, "deadline": deadline})
            return DexQuote("8453", "Aerodrome", sell_token, buy_token, Decimal(sell_amount), Decimal(out), gas_native, Decimal(0), Decimal("0"), Decimal(slippage_bps), Decimal(str((time.perf_counter() - quote_started) * 1000))), execution
        raise ValueError(f"unsupported direct DEX source: {source}")

    def close(self) -> None:
        w3 = getattr(self, "w3", None)
        provider = getattr(w3, "provider", None)
        if provider and hasattr(provider, "disconnect"):
            provider.disconnect()
