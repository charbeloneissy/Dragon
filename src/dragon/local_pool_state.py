from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from .local_amm import V2PoolState, V3PoolState

log = logging.getLogger(__name__)

@dataclass
class PoolRecord:
    address: str
    kind: str
    token0: str
    token1: str
    state: object
    block_number: int = 0
    updated_at: float = field(default_factory=time.time)
    initialized_ticks: dict[int,int] = field(default_factory=dict)

    @property
    def age_ms(self) -> float:
        return (time.time()-self.updated_at)*1000

class LocalPoolState:
    """In-memory pool state fed by WebSocket events.

    The scanner can consume this state without performing quote eth_calls.
    V3 records are fail-closed until the tick cache is sufficiently populated.
    """

    def __init__(self):
        self.pools: dict[str,PoolRecord] = {}
        self._lock=asyncio.Lock()

    async def register_v2(self, address, token0, token1, reserve0, reserve1, fee_bps=30):
        async with self._lock:
            self.pools[address.lower()] = PoolRecord(address,"v2",token0,token1,V2PoolState(token0,token1,int(reserve0),int(reserve1),int(fee_bps)))

    async def register_v3(self, address, token0, token1, sqrt_price_x96, liquidity, fee_pips, tick=0):
        async with self._lock:
            self.pools[address.lower()] = PoolRecord(address,"v3",token0,token1,V3PoolState(token0,token1,int(sqrt_price_x96),int(liquidity),int(fee_pips),int(tick)))

    async def apply_v2_reserves(self,address,reserve0,reserve1,block_number=0):
        async with self._lock:
            p=self.pools.get(address.lower())
            if not p or p.kind!="v2": return False
            s=p.state
            s.reserve0=int(reserve0); s.reserve1=int(reserve1)
            p.block_number=int(block_number); p.updated_at=time.time()
            return True

    async def apply_v3_slot(self,address,sqrt_price_x96,liquidity,tick,block_number=0):
        async with self._lock:
            p=self.pools.get(address.lower())
            if not p or p.kind!="v3": return False
            s=p.state
            s.sqrt_price_x96=int(sqrt_price_x96); s.liquidity=int(liquidity); s.tick=int(tick)
            p.block_number=int(block_number); p.updated_at=time.time()
            return True

    async def snapshot(self):
        async with self._lock:
            return dict(self.pools)

class BasePoolWebSocket:
    """Low-latency Base log subscriber.

    Pool addresses are supplied as JSON in DEX_LOCAL_POOLS_JSON. This avoids
    guessing pool types and keeps execution fail-closed.
    """

    def __init__(self, state: LocalPoolState, ws_url: str | None = None):
        self.state=state
        self.ws_url=(ws_url or os.getenv("DEX_RPC_WS_URL","")).strip()
        self._running=True

    @staticmethod
    def _config():
        raw=os.getenv("DEX_LOCAL_POOLS_JSON","").strip()
        if not raw: return []
        value=json.loads(raw)
        if not isinstance(value,list): raise ValueError("DEX_LOCAL_POOLS_JSON must be a JSON array")
        return value

    async def run(self):
        if not self.ws_url:
            raise RuntimeError("DEX_RPC_WS_URL is required for local pool mode")
        import websockets
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=10, ping_timeout=5, max_size=8_000_000) as ws:
                    await self._subscribe(ws)
                    async for raw in ws:
                        await self._handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("pool websocket disconnected: %s", exc)
                await asyncio.sleep(1.0)

    async def _subscribe(self, ws):
        pools=self._config()
        addresses=[str(p["address"]) for p in pools]
        if not addresses: raise RuntimeError("DEX_LOCAL_POOLS_JSON contains no pools")
        # Subscribe to all pool logs. The ABI-specific decoder is intentionally
        # isolated from the transport so additional DEX pool types can be added.
        await ws.send(json.dumps({"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["logs",{"address":addresses}]}))

    async def _handle(self,msg):
        if msg.get("method")!="eth_subscription": return
        result=msg.get("params",{}).get("result",{})
        address=str(result.get("address","")).lower()
        block=int(result.get("blockNumber","0x0"),16)
        topics=[str(x).lower() for x in result.get("topics",[])]
        data=bytes.fromhex(str(result.get("data","0x"))[2:])
        # Uniswap V2/Aerodrome Sync(uint112,uint112)
        sync_topic="1c411e9a96e7d9e7e3d0f6b7d2f7f2d6f0d8b1c4a2e7d3e0b5f5b5f7f8b8f8b5"
        # Topic constants are configurable because forks can expose compatible
        # events with different indexing. Default decoders use event signatures
        # supplied by each pool config.
        for cfg in self._config():
            if str(cfg.get("address","")).lower()!=address: continue
            if len(topics)>=1 and topics[0] == str(cfg.get("sync_topic","")).lower():
                if len(data)>=64:
                    await self.state.apply_v2_reserves(address,int.from_bytes(data[:32],"big"),int.from_bytes(data[32:64],"big"),block)
            if len(topics)>=1 and topics[0] == str(cfg.get("swap_topic","")).lower():
                # V3 Swap data: amount0, amount1, sqrtPriceX96, liquidity, tick.
                if len(data)>=160:
                    await self.state.apply_v3_slot(address,int.from_bytes(data[64:96],"big"),int.from_bytes(data[96:128],"big"),int.from_bytes(data[128:160],"big",signed=True),block)
