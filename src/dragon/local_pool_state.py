from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field

from .local_amm import V2PoolState, V3PoolState

log = logging.getLogger(__name__)

V2_SYNC_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"
V3_SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V3_MINT_TOPIC = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
V3_BURN_TOPIC = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982"
V3_INITIALIZE_TOPIC = "0x98636036cb66a9c19a37435efc1e90142190214e8abeb821bdba3f2990dd4c95"


@dataclass
class PoolRecord:
    address: str
    kind: str
    token0: str
    token1: str
    state: object
    block_number: int = 0
    updated_at: float = field(default_factory=time.time)
    initialized_ticks: dict[int, int] = field(default_factory=dict)

    @property
    def age_ms(self) -> float:
        return (time.time() - self.updated_at) * 1000


class LocalPoolState:
    """In-memory pool state. Quotes never mutate live state."""

    def __init__(self):
        self.pools: dict[str, PoolRecord] = {}
        self._lock = asyncio.Lock()

    async def register_v2(self, address, token0, token1, reserve0, reserve1, fee_bps=30):
        async with self._lock:
            self.pools[address.lower()] = PoolRecord(
                address, "v2", token0, token1,
                V2PoolState(token0, token1, int(reserve0), int(reserve1), int(fee_bps))
            )

    async def register_v3(self, address, token0, token1, sqrt_price_x96, liquidity,
                          fee_pips, tick=0, initialized_ticks=None):
        ticks = {int(k): int(v) for k, v in (initialized_ticks or {}).items()}
        async with self._lock:
            self.pools[address.lower()] = PoolRecord(
                address, "v3", token0, token1,
                V3PoolState(token0, token1, int(sqrt_price_x96), int(liquidity),
                            int(fee_pips), int(tick), ticks),
                initialized_ticks=ticks,
            )

    async def apply_v2_reserves(self, address, reserve0, reserve1, block_number=0):
        async with self._lock:
            p = self.pools.get(address.lower())
            if not p or p.kind != "v2":
                return False
            p.state.reserve0 = int(reserve0)
            p.state.reserve1 = int(reserve1)
            p.block_number = int(block_number)
            p.updated_at = time.time()
            return True

    async def apply_v3_slot(self, address, sqrt_price_x96, liquidity, tick, block_number=0):
        async with self._lock:
            p = self.pools.get(address.lower())
            if not p or p.kind != "v3":
                return False
            p.state.sqrt_price_x96 = int(sqrt_price_x96)
            p.state.liquidity = int(liquidity)
            p.state.tick = int(tick)
            p.block_number = int(block_number)
            p.updated_at = time.time()
            return True

    async def apply_v3_tick_delta(self, address, tick, liquidity_delta, block_number=0):
        async with self._lock:
            p = self.pools.get(address.lower())
            if not p or p.kind != "v3":
                return False
            tick = int(tick)
            p.initialized_ticks[tick] = p.initialized_ticks.get(tick, 0) + int(liquidity_delta)
            p.state.initialized_ticks = p.initialized_ticks
            if p.initialized_ticks[tick] == 0:
                p.initialized_ticks.pop(tick, None)
            p.block_number = max(p.block_number, int(block_number))
            p.updated_at = time.time()
            return True

    async def snapshot(self):
        async with self._lock:
            # PoolRecord/state are mutable, so create immutable-ish state copies.
            out = {}
            for address, p in self.pools.items():
                if p.kind == "v2":
                    s = p.state
                    state = V2PoolState(s.token0, s.token1, s.reserve0, s.reserve1, s.fee_bps)
                else:
                    s = p.state
                    ticks = dict(p.initialized_ticks)
                    state = V3PoolState(s.token0, s.token1, s.sqrt_price_x96,
                                         s.liquidity, s.fee_pips, s.tick, ticks)
                out[address] = PoolRecord(p.address, p.kind, p.token0, p.token1, state,
                                          p.block_number, p.updated_at, dict(p.initialized_ticks))
            return out


class BasePoolWebSocket:
    """Base log subscriber with V2/V3 liquidity-state decoding."""

    def __init__(self, state: LocalPoolState, ws_url: str | None = None):
        self.state = state
        self.ws_url = (ws_url or os.getenv("DEX_RPC_WS_URL", "")).strip()
        self._running = True

    @staticmethod
    def _config():
        raw = os.getenv("DEX_LOCAL_POOLS_JSON", "").strip()
        if not raw:
            return []
        value = json.loads(raw)
        if not isinstance(value, list):
            raise ValueError("DEX_LOCAL_POOLS_JSON must be a JSON array")
        return value

    async def bootstrap(self):
        for cfg in self._config():
            kind = str(cfg.get("kind", "")).lower()
            if kind == "v2":
                await self.state.register_v2(
                    cfg["address"], cfg["token0"], cfg["token1"],
                    cfg.get("reserve0", 0), cfg.get("reserve1", 0), cfg.get("fee_bps", 30)
                )
            elif kind == "v3":
                await self.state.register_v3(
                    cfg["address"], cfg["token0"], cfg["token1"],
                    cfg.get("sqrt_price_x96", 0), cfg.get("liquidity", 0),
                    cfg.get("fee_pips", 3000), cfg.get("tick", 0),
                    cfg.get("initialized_ticks", {})
                )
        log.info("local pool bootstrap complete pools=%s", len(self.state.pools))

    async def run(self):
        if not self.ws_url:
            raise RuntimeError("DEX_RPC_WS_URL is required for local pool mode")
        import websockets
        await self.bootstrap()
        while self._running:
            try:
                async with websockets.connect(
                    self.ws_url, ping_interval=10, ping_timeout=5, max_size=8_000_000
                ) as ws:
                    await self._subscribe(ws)
                    async for raw in ws:
                        await self._handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("pool websocket disconnected: %s", exc)
                await asyncio.sleep(1.0)

    async def _subscribe(self, ws):
        pools = self._config()
        addresses = [str(p["address"]) for p in pools]
        if not addresses:
            raise RuntimeError("DEX_LOCAL_POOLS_JSON contains no pools")
        await ws.send(json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
            "params": ["logs", {"address": addresses}]
        }))

    @staticmethod
    def _signed_topic(value: str, bits: int) -> int:
        raw = int(value, 16)
        if raw & (1 << (bits - 1)):
            raw -= 1 << bits
        return raw

    async def _handle(self, msg):
        if msg.get("method") != "eth_subscription":
            return
        result = msg.get("params", {}).get("result", {})
        address = str(result.get("address", "")).lower()
        block = int(result.get("blockNumber", "0x0"), 16)
        topics = [str(x).lower() for x in result.get("topics", [])]
        data = bytes.fromhex(str(result.get("data", "0x"))[2:])

        for cfg in self._config():
            if str(cfg.get("address", "")).lower() != address or not topics:
                continue
            topic0 = topics[0]
            sync_topic = str(cfg.get("sync_topic", V2_SYNC_TOPIC)).lower()
            swap_topic = str(cfg.get("swap_topic", V3_SWAP_TOPIC)).lower()
            mint_topic = str(cfg.get("mint_topic", V3_MINT_TOPIC)).lower()
            burn_topic = str(cfg.get("burn_topic", V3_BURN_TOPIC)).lower()
            init_topic = str(cfg.get("initialize_topic", V3_INITIALIZE_TOPIC)).lower()

            if topic0 == sync_topic and len(data) >= 64:
                await self.state.apply_v2_reserves(
                    address, int.from_bytes(data[:32], "big"),
                    int.from_bytes(data[32:64], "big"), block
                )
                continue

            if topic0 == swap_topic and len(data) >= 160:
                await self.state.apply_v3_slot(
                    address,
                    int.from_bytes(data[64:96], "big"),
                    int.from_bytes(data[96:128], "big"),
                    int.from_bytes(data[128:160], "big", signed=True),
                    block
                )
                continue

            # Mint/Burn: topics[2]=tickLower, topics[3]=tickUpper, data[:16]=liquidity.
            if topic0 in {mint_topic, burn_topic} and len(topics) >= 4 and len(data) >= 16:
                lower = self._signed_topic(topics[2], 256)
                upper = self._signed_topic(topics[3], 256)
                # int24 sign extension is present in the ABI topic.
                lower = lower & ((1 << 24) - 1)
                upper = upper & ((1 << 24) - 1)
                if lower & (1 << 23):
                    lower -= 1 << 24
                if upper & (1 << 23):
                    upper -= 1 << 24
                amount = int.from_bytes(data[:16], "big")
                delta = amount if topic0 == mint_topic else -amount
                await self.state.apply_v3_tick_delta(address, lower, delta, block)
                await self.state.apply_v3_tick_delta(address, upper, -delta, block)
                continue

            if topic0 == init_topic and len(data) >= 64:
                snap = await self.state.snapshot()
                p = snap.get(address)
                if p:
                    await self.state.apply_v3_slot(
                        address,
                        int.from_bytes(data[:32], "big"),
                        p.state.liquidity,
                        int.from_bytes(data[32:64], "big", signed=True),
                        block
                    )
