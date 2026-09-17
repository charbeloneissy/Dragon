from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.dex_0x import ZeroXAdapter
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine
from src.dragon.flash_executor import AaveFlashExecutor

STATE={"status":"starting","mode":"paper","chain_id":None,"sources":[],"scans":0,"opportunities":0,"last_scan":None,"last_error":None,"started_at":time.time(),"quote_amount":None,"quote_decimals":None,"min_net_profit":None,"safety_buffer":None,"own_capital":"0","flash_liquidity":None,"flash_loan_enabled":False,"last_tx_hash":None,"rejections":{}}
LOCK=Lock()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/","/health","/healthz"):
            with LOCK: payload=dict(STATE); payload["rejections"]=dict(STATE["rejections"])
            body=json.dumps(payload,default=str).encode(); self.send_response(200 if payload["status"] in {"starting","running","degraded"} else 503); self.send_header("Content-Type","application/json"); self.send_header("Cache-Control","no-store"); self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_response(404); self.end_headers()
    def log_message(self,*_args): return

def start_health_server():
    ThreadingHTTPServer(("0.0.0.0",int(os.getenv("PORT","10000"))),Handler).serve_forever()

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
    Thread(target=start_health_server,daemon=True).start(); adapter=ZeroXAdapter()
    try:
        if not adapter.enabled: raise RuntimeError("ZEROX_API_KEY is required for the real DEX adapter")
        chain_id=int(os.getenv("DEX_CHAIN_ID","8453")); taker_config=validate_evm_address("DEX_TAKER_ADDRESS",env_required("DEX_TAKER_ADDRESS")); quote_token=validate_evm_address("DEX_QUOTE_TOKEN",env_required("DEX_QUOTE_TOKEN")); base_token=validate_evm_address("DEX_BASE_TOKEN",env_required("DEX_BASE_TOKEN"))
        if quote_token.lower()==base_token.lower(): raise ValueError("DEX_QUOTE_TOKEN and DEX_BASE_TOKEN must be different")
        quote_decimals=int(os.getenv("DEX_QUOTE_TOKEN_DECIMALS","6")); own_capital=env_decimal("DEX_OWN_CAPITAL_QUOTE","0")
        if own_capital!=0: raise ValueError("DEX_OWN_CAPITAL_QUOTE must remain exactly 0")
        flash_cap=env_decimal("DEX_FLASH_LOAN_LIQUIDITY_QUOTE","100"); live=env_bool("LIVE_TRADING",False)
        if flash_cap<0: raise ValueError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE cannot be negative")
        min_profit=env_decimal("DEX_MIN_NET_PROFIT","0.005"); safety=env_decimal("DEX_SAFETY_BUFFER","0.001")
        if min_profit<Decimal("0.005"): raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.005")
        if safety<0: raise ValueError("DEX_SAFETY_BUFFER cannot be negative")
        slippage=int(os.getenv("DEX_SLIPPAGE_BPS","50")); latency=env_decimal("DEX_MAX_QUOTE_LATENCY_MS","1000")
        if not 0<=slippage<=5000 or latency<=0: raise ValueError("invalid slippage/latency configuration")
        flash_enabled=env_bool("FLASH_LOAN_ENABLED",False); configured_fee=env_decimal("FLASH_LOAN_FEE_BPS","0")
        if live:
            if not flash_enabled: raise RuntimeError("LIVE_TRADING requires FLASH_LOAN_ENABLED=true")
            if not env_bool("MEV_PROTECTION_REQUIRED",True) or not env_bool("ATOMIC_REPAYMENT_REQUIRED",True): raise RuntimeError("live execution requires MEV protection and atomic repayment")
            executor=AaveFlashExecutor(); taker=executor.config.executor_address; fee_bps=executor.flash_loan_fee_bps()
        else:
            executor=None; taker=taker_config; fee_bps=configured_fee
        if not flash_enabled and configured_fee!=0: raise ValueError("FLASH_LOAN_FEE_BPS requires FLASH_LOAN_ENABLED=true")
        available=set(adapter.sources(chain_id)); configured=tuple(x.strip() for x in os.getenv("DEX_SOURCES","").split(",") if x.strip()); requested=configured or ("Uniswap_V3","Aerodrome","SushiSwap","Uniswap_V2","PancakeSwapV3")
        sources=tuple(x for x in requested if x in available); unsupported=tuple(x for x in requested if x not in available)
        if unsupported: logging.warning("Ignoring unsupported DEX sources on chain %s: %s",chain_id,unsupported)
        if len(sources)<2: raise RuntimeError(f"fewer than two usable DEX sources found on chain {chain_id}: requested={requested}, available={sorted(available)}, usable={sources}")
        sources=sources[:4]
        engine=DexCrossExchangeEngine(adapter,sources,min_profit=min_profit,quote_token_decimals=quote_decimals,max_quote_latency_ms=latency,safety_buffer_quote=safety,flash_loan_enabled=flash_enabled,flash_loan_fee_bps=fee_bps)
        with LOCK: STATE.update({"status":"running","mode":"live" if live else "paper","chain_id":chain_id,"sources":list(sources),"quote_decimals":quote_decimals,"min_net_profit":str(min_profit),"safety_buffer":str(safety),"own_capital":"0","flash_liquidity":str(flash_cap),"flash_loan_enabled":flash_enabled})
        while True:
            try:
                if live:
                    actual=Decimal(executor.available_liquidity_units(quote_token))/(Decimal(10)**quote_decimals); max_quote=actual; fee_bps=executor.flash_loan_fee_bps(); engine.flash_loan_fee_bps=fee_bps
                    if max_quote<=0: raise RuntimeError("no flash-loan liquidity available for the quote token")
                    with LOCK: STATE["flash_liquidity"]=str(max_quote)
                else: max_quote=flash_cap
                opportunities=await asyncio.to_thread(engine.scan_max_profitable,chain_id=chain_id,quote_token=quote_token,base_token=base_token,max_quote_amount=max_quote,taker=taker,slippage_bps=slippage)
                merge_rejections(engine.last_rejections)
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
    finally: adapter.close()

if __name__=="__main__": asyncio.run(main())
