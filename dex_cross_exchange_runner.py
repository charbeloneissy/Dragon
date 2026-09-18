from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.dex_direct import DirectDexAdapter
from src.dragon.dex_composite import CompositeDexAdapter
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine
from src.dragon.flash_executor import AaveFlashExecutor
from src.dragon.base_pool_discovery import BASE_WETH, discover_recent_base_tokens

STATE={"status":"starting","mode":"paper","chain_id":None,"sources":[],"scans":0,"opportunities":0,"last_scan":None,"last_error":None,"started_at":time.time(),"quote_amount":None,"quote_decimals":None,"min_net_profit":None,"safety_buffer":None,"own_capital":"0","flash_liquidity":None,"flash_loan_enabled":False,"last_tx_hash":None,"rejections":{},"base_tokens":[]}
LOCK=Lock()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=self.path.split("?",1)[0]
        with LOCK:
            payload=dict(STATE)
            payload["rejections"]=dict(STATE["rejections"])
        if path=="/dashboard":
            try:
                with open("dashboard.html","rb") as f: body=f.read()
                self.send_response(200); self.send_header("Content-Type","text/html; charset=utf-8")
                self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
            except OSError as exc:
                body=json.dumps({"error":"dashboard unavailable","detail":str(exc)}).encode()
                self.send_response(500); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if path in ("/","/health","/healthz","/api/status"):
            body=json.dumps(payload,default=str).encode()
            self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self,*_args): return

class DragonHTTPServer(ThreadingHTTPServer):
    allow_reuse_address=True
    daemon_threads=True

def start_health_server():
    port=int(os.getenv("PORT","10000"))
    server=DragonHTTPServer(("0.0.0.0",port),Handler)
    logging.info("Dragon HTTP server listening on 0.0.0.0:%s",port)
    Thread(target=server.serve_forever,daemon=True).start()
    return server

def env_required(name):
    value=os.getenv(name,"").strip()
    if not value: raise RuntimeError(f"{name} is required")
    return value

def validate_evm_address(name,value):
    value=value.strip()
    if len(value)!=42 or not value.startswith("0x"): raise ValueError(f"{name} must be a 20-byte EVM address")
    try: int(value[2:],16)
    except ValueError as exc: raise ValueError(f"{name} contains non-hex characters") from exc
    return value

def env_decimal(name,default):
    try: value=Decimal(os.getenv(name,default))
    except InvalidOperation as exc: raise ValueError(f"{name} must be a decimal number") from exc
    if not value.is_finite(): raise ValueError(f"{name} must be finite")
    return value

def env_bool(name,default=False): return os.getenv(name,str(default)).strip().lower() in {"1","true","yes","on"}

def merge_rejections(stats):
    with LOCK:
        for key,value in stats.items(): STATE["rejections"][key]=int(value)

async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(message)s")
    start_health_server()
    adapter=None
    try:
        logging.info("Dragon scanner boot: initializing direct DEX adapter")
        direct_adapter=DirectDexAdapter()
        adapter=CompositeDexAdapter(direct_adapter)
        logging.info("Multi-DEX adapter connected: sources=%s", adapter.sources(int(os.getenv("DEX_CHAIN_ID","8453"))))
        chain_id=int(os.getenv("DEX_CHAIN_ID","8453")); taker_config=validate_evm_address("DEX_TAKER_ADDRESS",env_required("DEX_TAKER_ADDRESS")); quote_token=validate_evm_address("DEX_QUOTE_TOKEN",env_required("DEX_QUOTE_TOKEN")); base_token_raw=os.getenv("DEX_BASE_TOKEN","").strip(); base_token=validate_evm_address("DEX_BASE_TOKEN",base_token_raw) if base_token_raw else ""
        raw_base_tokens=os.getenv("DEX_BASE_TOKENS","").strip()
        configured_base_tokens=[validate_evm_address("DEX_BASE_TOKENS",x.strip()) for x in raw_base_tokens.split(",") if x.strip()] if raw_base_tokens else []
        base_tokens=[]
        for token in configured_base_tokens:
            if token.lower()!=quote_token.lower() and token.lower() not in {x.lower() for x in base_tokens}: base_tokens.append(token)
        auto_discovery=env_bool("DEX_AUTO_DISCOVERY",True)
        discovery_lookback=int(os.getenv("DEX_DISCOVERY_BLOCKS","250000"))
        discovery_chunk=int(os.getenv("DEX_DISCOVERY_CHUNK_BLOCKS","10000"))
        discovery_max=int(os.getenv("DEX_DISCOVERY_MAX_TOKENS","250"))
        discovery_refresh=float(os.getenv("DEX_DISCOVERY_REFRESH_SECONDS","300"))
        if auto_discovery:
            discovered=discover_recent_base_tokens(adapter,quote_token=quote_token,anchors=(quote_token, BASE_WETH),lookback_blocks=discovery_lookback,chunk_blocks=discovery_chunk,max_tokens=discovery_max)
            for token in discovered:
                if token.lower()!=quote_token.lower() and token.lower() not in {x.lower() for x in base_tokens}: base_tokens.append(token)
        if not base_tokens: raise ValueError("No DEX base tokens configured or discovered")
        base_tokens=base_tokens[:max(1,int(os.getenv("DEX_MAX_BASE_TOKENS","250")))]
        quote_decimals=int(os.getenv("DEX_QUOTE_TOKEN_DECIMALS","6")); own_capital=env_decimal("DEX_OWN_CAPITAL_QUOTE","0")
        if own_capital<0: raise ValueError("DEX_OWN_CAPITAL_QUOTE cannot be negative")
        flash_cap=env_decimal("DEX_FLASH_LOAN_LIQUIDITY_QUOTE","100"); live=env_bool("LIVE_TRADING",False)
        if own_capital>0 and live: raise ValueError("owned-capital live execution is not enabled by the current executor; keep DEX_OWN_CAPITAL_QUOTE=0 until an owned-capital executor is installed")
        if flash_cap<0: raise ValueError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE cannot be negative")
        min_profit=env_decimal("DEX_MIN_NET_PROFIT","0.005"); safety=env_decimal("DEX_SAFETY_BUFFER","0.001")
        if min_profit<Decimal("0.005"): raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.005")
        if safety<0: raise ValueError("DEX_SAFETY_BUFFER cannot be negative")
        slippage=int(os.getenv("DEX_SLIPPAGE_BPS","50")); latency=env_decimal("DEX_MAX_QUOTE_LATENCY_MS","1000")
        if not 0<=slippage<=5000 or latency<=0: raise ValueError("invalid slippage/latency configuration")
        flash_enabled=env_bool("FLASH_LOAN_ENABLED",False) and own_capital==0; configured_fee=env_decimal("FLASH_LOAN_FEE_BPS","0")
        if live:
            if not flash_enabled: raise RuntimeError("LIVE_TRADING requires FLASH_LOAN_ENABLED=true")
            if not env_bool("MEV_PROTECTION_REQUIRED",True) or not env_bool("ATOMIC_REPAYMENT_REQUIRED",True): raise RuntimeError("live execution requires MEV protection and atomic repayment")
            executor=AaveFlashExecutor(); taker=executor.config.executor_address; fee_bps=executor.flash_loan_fee_bps()
        else:
            executor=None; taker=taker_config
            fee_bps=adapter.flash_loan_fee_bps() if flash_enabled else configured_fee
        if not flash_enabled and configured_fee!=0: raise ValueError("FLASH_LOAN_FEE_BPS requires FLASH_LOAN_ENABLED=true")
        available=set(adapter.sources(chain_id)); configured=tuple(x.strip() for x in os.getenv("DEX_SOURCES","").split(",") if x.strip()); requested=configured or adapter.sources(chain_id)
        sources=tuple(x for x in requested if x in available); unsupported=tuple(x for x in requested if x not in available)
        if unsupported: logging.warning("Ignoring unsupported DEX sources on chain %s: %s",chain_id,unsupported)
        if len(sources)<2: raise RuntimeError(f"fewer than two usable DEX sources found on chain {chain_id}: requested={requested}, available={sorted(available)}, usable={sources}")
        sources=sources[:max(2,min(12,int(os.getenv("DEX_MAX_SOURCES","8")))]
        engine_kwargs=dict(
            min_profit=min_profit,
            quote_token_decimals=quote_decimals,
            max_quote_latency_ms=latency,
            safety_buffer_quote=safety,
            flash_loan_enabled=flash_enabled,
            flash_loan_fee_bps=fee_bps,
        )
        # Build one isolated engine per token once. Recreating engines inside every
        # scan cycle repeatedly cloned Web3/RPC adapters and defeated the latency optimization.
        token_engines={
            token: DexCrossExchangeEngine(adapter,sources,**engine_kwargs)
            for token in base_tokens
        }
        with LOCK: STATE.update({"status":"running","mode":"live" if live else "paper","chain_id":chain_id,"sources":list(sources),"base_tokens":base_tokens,"universe_refreshed_at":time.time(),"quote_decimals":quote_decimals,"min_net_profit":str(min_profit),"safety_buffer":str(safety),"own_capital":str(own_capital),"flash_liquidity":str(own_capital if own_capital>0 else flash_cap),"flash_loan_enabled":flash_enabled})
        while True:
            try:
                if auto_discovery and time.time()-float(STATE.get("universe_refreshed_at") or 0) >= discovery_refresh:
                    discovered=discover_recent_base_tokens(adapter,quote_token=quote_token,anchors=(quote_token, BASE_WETH),lookback_blocks=discovery_lookback,chunk_blocks=discovery_chunk,max_tokens=discovery_max)
                    current=list(base_tokens)
                    for token in discovered:
                        if token.lower()!=quote_token.lower() and token.lower() not in {x.lower() for x in current}: current.append(token)
                    base_tokens=current[:max(1,int(os.getenv("DEX_MAX_BASE_TOKENS","250")))]
                    for token in list(token_engines):
                        if token not in base_tokens: del token_engines[token]
                    for token in base_tokens:
                        if token not in token_engines: token_engines[token]=DexCrossExchangeEngine(adapter,sources,**engine_kwargs)
                    with LOCK:
                        STATE["base_tokens"]=base_tokens
                        STATE["universe_refreshed_at"]=time.time()
                    logging.info("DEX universe refreshed base_tokens=%s",len(base_tokens))
                if live:
                    actual=Decimal(executor.available_liquidity_units(quote_token))/(Decimal(10)**quote_decimals); max_quote=actual; fee_bps=executor.flash_loan_fee_bps()
                    for token_engine in token_engines.values():
                        token_engine.flash_loan_fee_bps=fee_bps
                    if max_quote<=0: raise RuntimeError("no flash-loan liquidity available for the quote token")
                    with LOCK: STATE["flash_liquidity"]=str(max_quote)
                else: max_quote=own_capital if own_capital>0 else flash_cap
                all_opportunities=[]
                aggregate_rejections={}
                scan_concurrency=max(1,min(len(base_tokens),int(os.getenv("DEX_SCAN_CONCURRENCY","4"))))
                scan_semaphore=asyncio.Semaphore(scan_concurrency)
                async def scan_base_token(scan_base):
                    async with scan_semaphore:
                        # Each token has its own persistent engine state, so scans can
                        # run concurrently without rebuilding RPC/Web3 adapters.
                        scan_engine=token_engines[scan_base]
                        scan_engine.flash_loan_fee_bps=fee_bps
                        found=await asyncio.to_thread(
                            scan_engine.scan_max_profitable,
                            chain_id=chain_id,
                            quote_token=quote_token,
                            base_token=scan_base,
                            max_quote_amount=max_quote,
                            taker=taker,
                            slippage_bps=slippage,
                        )
                        return scan_base, found, dict(scan_engine.last_rejections)
                scan_results=await asyncio.gather(*(scan_base_token(scan_base) for scan_base in base_tokens), return_exceptions=True)
                for result in scan_results:
                    if isinstance(result, Exception):
                        logging.exception("DEX base-token scan failed", exc_info=result)
                        aggregate_rejections["base_token_scan_error"]=aggregate_rejections.get("base_token_scan_error",0)+1
                        continue
                    scan_base, found, rejections=result
                    all_opportunities.extend(found)
                    for key,value in rejections.items():
                        aggregate_rejections[key]=aggregate_rejections.get(key,0)+int(value)
                all_opportunities.sort(key=lambda x: x.net_profit_quote, reverse=True)
                opportunities=all_opportunities[:max(1,int(os.getenv("DEX_MAX_OPPORTUNITIES","8")))]
                merge_rejections(aggregate_rejections)
                logging.info("DEX scan complete: sources=%s base_tokens=%s candidates_per_token=%s opportunities=%s rejections=%s", sources, len(base_tokens), len(next(iter(token_engines.values()))._candidate_amounts(int(max_quote * (Decimal(10) ** quote_decimals)))), len(opportunities), aggregate_rejections)
                with LOCK: STATE["scans"]+=1; STATE["opportunities"]+=len(opportunities); STATE["last_scan"]=time.time(); STATE["last_error"]=None; STATE["quote_amount"]=str(opportunities[0].quote_amount/(Decimal(10)**quote_decimals)) if opportunities else None
                if opportunities and live:
                    best=opportunities[0]; max_block=executor.w3.eth.block_number+max(1,int(os.getenv("DEX_MAX_BLOCKS_AHEAD","2")))
                    tx_hash=await asyncio.to_thread(executor.build_and_send,best,max_block_number=max_block); receipt=await asyncio.to_thread(executor.wait_for_success,tx_hash,int(os.getenv("DEX_TX_RECEIPT_TIMEOUT","30")))
                    with LOCK: STATE["last_tx_hash"]=tx_hash
                    logging.info("ATOMIC FLASH TX CONFIRMED hash=%s gas_used=%s",tx_hash,receipt.get("gasUsed")); await asyncio.sleep(float(os.getenv("DEX_LIVE_COOLDOWN_SECONDS","1.0")))
                else: await asyncio.sleep(float(os.getenv("DEX_POLL_SECONDS","0.5")))
            except Exception as exc:
                logging.exception("DEX scan/execution failed")
                with LOCK: STATE["status"]="degraded"; STATE["last_error"]=str(exc)
                await asyncio.sleep(2)
                with LOCK: STATE["status"]="running"
    finally:
        if adapter is not None:
            try:
                adapter.close()
            except Exception:
                logging.exception("failed to close DEX adapter")

if __name__=="__main__": asyncio.run(main())
