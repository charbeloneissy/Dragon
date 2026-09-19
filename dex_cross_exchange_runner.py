from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock, Thread

from src.dragon.dex_direct import DirectDexAdapter, RpcRateLimitError
from src.dragon.dex_composite import CompositeDexAdapter
from src.dragon.dex_cross_exchange import DexCrossExchangeEngine
from src.dragon.flash_executor import AaveFlashExecutor, FlashTransactionReverted
from src.dragon.base_pool_discovery import BASE_WETH, discover_recent_base_tokens
from src.dragon.observability import ExecutionTelemetry

STATE={"status":"starting","mode":"paper","chain_id":None,"sources":[],"scans":0,"opportunities":0,"last_scan":None,"last_error":None,"started_at":time.time(),"quote_amount":None,"quote_decimals":None,"min_net_profit":None,"safety_buffer":None,"own_capital":"0","flash_liquidity":None,"flash_pool_liquidity":None,"flash_cap":None,"flash_loan_enabled":False,"compounding_enabled":False,"compound_reserve_quote":"0","compound_amount_quote":"0","flash_loan_amount_quote":None,"last_tx_hash":None,"rejections":{},"base_tokens":[],"opportunity_records":[],"data_source":"direct executable quotes; pool event counter is zero unless a local pool stream is enabled"}
LOCK=Lock()
METRICS=ExecutionTelemetry()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path=self.path.split("?",1)[0]
        with LOCK:
            payload=dict(STATE)
            payload["rejections"]=dict(STATE["rejections"])
            payload["observability"]=METRICS.snapshot()
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


def opportunity_view(opportunity, identifier):
    return {
        "id": identifier,
        "source": "base-dex",
        "buy_source": opportunity.buy_source,
        "sell_source": opportunity.sell_source,
        "base_token": opportunity.base_token,
        "quote_token": opportunity.quote_token,
        "quote_amount": str(opportunity.quote_amount),
        "compound_amount": str(opportunity.compound_amount),
        "flash_loan_amount": str(opportunity.flash_loan_amount),
        "gross_profit_quote": str(opportunity.gross_profit_quote),
        "net_profit_quote": str(opportunity.net_profit_quote),
        "gas_cost_quote": str(opportunity.gas_cost_quote),
        "flash_loan_fee_quote": str(opportunity.flash_loan_fee_quote),
        "safety_buffer_quote": str(opportunity.safety_buffer_quote),
        "mev_buffer_quote": str(opportunity.safety_buffer_quote),
        "status": "ready_for_fresh_simulation",
    }

async def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(message)s")
    start_health_server()
    adapter=None
    try:
        logging.info("Dragon scanner boot: initializing direct DEX adapter")
        # Public RPCs can temporarily rate-limit during a deploy/restart. Do not
        # crash the Render service when that happens. Keep the health endpoint up
        # and retry with backoff until an endpoint becomes available.
        rpc_retry_delay = 1.0
        while adapter is None:
            try:
                direct_adapter = DirectDexAdapter()
                adapter = CompositeDexAdapter(direct_adapter)
            except RpcRateLimitError as exc:
                logging.warning("DEX RPC unavailable during startup; retrying in %.1fs: %s", rpc_retry_delay, exc)
                await asyncio.sleep(rpc_retry_delay)
                rpc_retry_delay = min(15.0, rpc_retry_delay * 2.0)
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
        discovery_max=int(os.getenv("DEX_DISCOVERY_MAX_TOKENS","40"))
        discovery_refresh=float(os.getenv("DEX_DISCOVERY_REFRESH_SECONDS","300"))
        if auto_discovery:
            discovered=discover_recent_base_tokens(adapter,quote_token=quote_token,anchors=(quote_token, BASE_WETH),lookback_blocks=discovery_lookback,chunk_blocks=discovery_chunk,max_tokens=discovery_max)
            for token in discovered:
                if token.lower()!=quote_token.lower() and token.lower() not in {x.lower() for x in base_tokens}: base_tokens.append(token)
        if not base_tokens: raise ValueError("No DEX base tokens configured or discovered")
        base_tokens=base_tokens[:max(1,int(os.getenv("DEX_MAX_BASE_TOKENS","8")))]
        quote_decimals=int(os.getenv("DEX_QUOTE_TOKEN_DECIMALS","6")); own_capital=env_decimal("DEX_OWN_CAPITAL_QUOTE","0")
        if own_capital<0: raise ValueError("DEX_OWN_CAPITAL_QUOTE cannot be negative")
        flash_cap=env_decimal("DEX_FLASH_LOAN_LIQUIDITY_QUOTE","100"); live=env_bool("LIVE_TRADING",False)
        compound_enabled=env_bool("DEX_COMPOUND_PROFITS",True)
        compound_ratio=env_decimal("DEX_COMPOUND_RATIO","1")
        compound_max=env_decimal("DEX_MAX_COMPOUND_QUOTE","100")
        if not 0 <= compound_ratio <= 1: raise ValueError("DEX_COMPOUND_RATIO must be between 0 and 1")
        if compound_max < 0: raise ValueError("DEX_MAX_COMPOUND_QUOTE cannot be negative")
        if own_capital>0 and live: raise ValueError("owned-capital live execution is not enabled by the current executor; keep DEX_OWN_CAPITAL_QUOTE=0 until an owned-capital executor is installed")
        if flash_cap<0: raise ValueError("DEX_FLASH_LOAN_LIQUIDITY_QUOTE cannot be negative")
        min_profit=env_decimal("DEX_MIN_NET_PROFIT","0.005"); safety=env_decimal("DEX_SAFETY_BUFFER","0.001")
        if min_profit<Decimal("0.005"): raise ValueError("DEX_MIN_NET_PROFIT cannot be below 0.005")
        if safety<0: raise ValueError("DEX_SAFETY_BUFFER cannot be negative")
        slippage=int(os.getenv("DEX_SLIPPAGE_BPS","50")); latency=env_decimal("DEX_MAX_QUOTE_LATENCY_MS","500")
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
        sources=sources[:max(2,min(12,int(os.getenv("DEX_MAX_SOURCES","8"))))]
        engine_kwargs=dict(
            min_profit=min_profit,
            quote_token_decimals=quote_decimals,
            max_quote_latency_ms=latency,
            safety_buffer_quote=safety,
            flash_loan_enabled=flash_enabled,
            flash_loan_fee_bps=fee_bps,
            telemetry=METRICS,
        )
        # Build one isolated engine per token once. Recreating engines inside every
        # scan cycle repeatedly cloned Web3/RPC adapters and defeated the latency optimization.
        token_engines={
            token: DexCrossExchangeEngine(adapter,sources,**engine_kwargs)
            for token in base_tokens
        }
        with LOCK: STATE.update({"status":"running","mode":"live" if live else "paper","chain_id":chain_id,"sources":list(sources),"base_tokens":base_tokens,"universe_refreshed_at":time.time(),"quote_decimals":quote_decimals,"min_net_profit":str(min_profit),"safety_buffer":str(safety),"own_capital":str(own_capital),"flash_liquidity":str(own_capital if own_capital>0 else flash_cap),"flash_cap":str(flash_cap),"flash_loan_enabled":flash_enabled,"compounding_enabled":bool(live and compound_enabled),"compound_ratio":str(compound_ratio),"compound_max_quote":str(compound_max)})
        while True:
            try:
                if auto_discovery and time.time()-float(STATE.get("universe_refreshed_at") or 0) >= discovery_refresh:
                    discovered=discover_recent_base_tokens(adapter,quote_token=quote_token,anchors=(quote_token, BASE_WETH),lookback_blocks=discovery_lookback,chunk_blocks=discovery_chunk,max_tokens=discovery_max)
                    current=list(base_tokens)
                    for token in discovered:
                        if token.lower()!=quote_token.lower() and token.lower() not in {x.lower() for x in current}: current.append(token)
                    base_tokens=current[:max(1,int(os.getenv("DEX_MAX_BASE_TOKENS","8")))]
                    for token in list(token_engines):
                        if token not in base_tokens: del token_engines[token]
                    for token in base_tokens:
                        if token not in token_engines: token_engines[token]=DexCrossExchangeEngine(adapter,sources,**engine_kwargs)
                    with LOCK:
                        STATE["base_tokens"]=base_tokens
                        STATE["universe_refreshed_at"]=time.time()
                    logging.info("DEX universe refreshed base_tokens=%s",len(base_tokens))
                if live:
                    scale=Decimal(10)**quote_decimals
                    pool_liquidity=Decimal(executor.available_liquidity_units(quote_token))/scale
                    actual=min(pool_liquidity,flash_cap)
                    reserve=Decimal(executor.compound_balance_units(quote_token))/scale if compound_enabled else Decimal("0")
                    compound_amount_quote=min(reserve*compound_ratio,compound_max)
                    compound_amount_raw=int(compound_amount_quote*scale)
                    max_quote=actual+compound_amount_quote; fee_bps=executor.flash_loan_fee_bps()
                    for token_engine in token_engines.values():
                        token_engine.flash_loan_fee_bps=fee_bps
                    if max_quote<=0: raise RuntimeError("no flash-loan liquidity available for the quote token")
                    with LOCK: STATE.update({"flash_liquidity":str(actual),"flash_pool_liquidity":str(pool_liquidity),"compound_reserve_quote":str(reserve),"compound_amount_quote":str(compound_amount_quote),"flash_loan_amount_quote":str(actual)})
                else:
                    max_quote=own_capital if own_capital>0 else flash_cap
                    compound_amount_raw=0
                    with LOCK: STATE.update({"flash_liquidity":str(max_quote),"flash_pool_liquidity":None,"compound_reserve_quote":"0","compound_amount_quote":"0","flash_loan_amount_quote":str(max_quote)})
                all_opportunities=[]
                aggregate_rejections={}
                # Fast scanner architecture: use a small live quote to rank the
                # dynamic universe, then spend expensive multi-size quotes only on
                # the strongest candidates. The full scan remains the profitability
                # and execution gate, so the fast probe can never authorize a trade.
                fast_probe_quote=env_decimal("DEX_FAST_PROBE_QUOTE","0.25")
                if fast_probe_quote <= 0:
                    raise ValueError("DEX_FAST_PROBE_QUOTE must be positive")
                probe_scale=Decimal(10) ** quote_decimals
                probe_amount=max(1,int(fast_probe_quote*probe_scale))
                fast_candidates=max(1,min(len(base_tokens),int(os.getenv("DEX_FAST_CANDIDATES","3"))))
                probe_concurrency=max(1,min(len(base_tokens),int(os.getenv("DEX_FAST_PROBE_CONCURRENCY","4"))))
                probe_semaphore=asyncio.Semaphore(probe_concurrency)

                async def probe_base_token(scan_base):
                    async with probe_semaphore:
                        scan_engine=token_engines[scan_base]
                        scan_engine.flash_loan_fee_bps=fee_bps
                        score=await asyncio.to_thread(
                            scan_engine.fast_probe,
                            chain_id=chain_id,
                            quote_token=quote_token,
                            base_token=scan_base,
                            quote_amount=probe_amount,
                            taker=taker,
                            slippage_bps=slippage,
                        )
                        return scan_base, score, dict(scan_engine.last_rejections)

                probe_results=await asyncio.gather(
                    *(probe_base_token(scan_base) for scan_base in base_tokens),
                    return_exceptions=True,
                )
                ranked=[]
                for result in probe_results:
                    if isinstance(result, Exception):
                        logging.exception("DEX fast probe failed", exc_info=result)
                        METRICS.increment("quote_failures")
                        aggregate_rejections["fast_probe_error"]=aggregate_rejections.get("fast_probe_error",0)+1
                        continue
                    scan_base, score, rejections=result
                    ranked.append((score,scan_base))
                    for key,value in rejections.items():
                        aggregate_rejections[key]=aggregate_rejections.get(key,0)+int(value)
                ranked.sort(key=lambda item:item[0], reverse=True)
                selected=[token for score,token in ranked if score.is_finite()][:fast_candidates]
                logging.info(
                    "FAST SCANNER ranked base_tokens=%s selected=%s probe_quote=%s top_gross_bps=%s",
                    len(base_tokens), selected, fast_probe_quote,
                    str(ranked[0][0]) if ranked and ranked[0][0].is_finite() else "none",
                )

                scan_concurrency=max(1,min(len(selected),int(os.getenv("DEX_SCAN_CONCURRENCY","2"))))
                scan_semaphore=asyncio.Semaphore(scan_concurrency)
                async def scan_base_token(scan_base):
                    async with scan_semaphore:
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
                            compound_amount=compound_amount_raw,
                        )
                        return scan_base, found, dict(scan_engine.last_rejections)
                scan_results=await asyncio.gather(
                    *(scan_base_token(scan_base) for scan_base in selected),
                    return_exceptions=True,
                )
                for result in scan_results:
                    if isinstance(result, Exception):
                        logging.exception("DEX base-token scan failed", exc_info=result)
                        METRICS.increment("quote_failures")
                        aggregate_rejections["base_token_scan_error"]=aggregate_rejections.get("base_token_scan_error",0)+1
                        continue
                    scan_base, found, rejections=result
                    all_opportunities.extend(found)
                    for key,value in rejections.items():
                        aggregate_rejections[key]=aggregate_rejections.get(key,0)+int(value)
                all_opportunities.sort(key=lambda x: x.net_profit_quote, reverse=True)
                opportunities=all_opportunities[:max(1,int(os.getenv("DEX_MAX_OPPORTUNITIES","8")))]
                opportunity_ids = {}
                opportunity_rows = []
                for opportunity in opportunities:
                    identifier = METRICS.record_opportunity(opportunity)
                    METRICS.mark(identifier, "ready_for_fresh_simulation")
                    opportunity_ids[id(opportunity)] = identifier
                    opportunity_rows.append(opportunity_view(opportunity, identifier))
                merge_rejections(aggregate_rejections)
                logging.info("DEX scan complete: sources=%s base_tokens=%s candidates_per_token=%s opportunities=%s rejections=%s", sources, len(base_tokens), len(next(iter(token_engines.values()))._candidate_amounts(int(max_quote * (Decimal(10) ** quote_decimals)))), len(opportunities), aggregate_rejections)
                with LOCK:
                    STATE["scans"]+=1
                    STATE["opportunities"]+=len(opportunities)
                    STATE["last_scan"]=time.time()
                    STATE["last_error"]=None
                    STATE["quote_amount"]=str(opportunities[0].quote_amount/(Decimal(10)**quote_decimals)) if opportunities else None
                    STATE["compound_amount_quote"]=str(Decimal(opportunities[0].compound_amount)/(Decimal(10)**quote_decimals)) if opportunities else STATE.get("compound_amount_quote","0")
                    STATE["flash_loan_amount_quote"]=str(Decimal(opportunities[0].flash_loan_amount)/(Decimal(10)**quote_decimals)) if opportunities else STATE.get("flash_loan_amount_quote")
                    STATE["opportunity_records"]=opportunity_rows
                if opportunities and live:
                    best=opportunities[0]
                    identifier=opportunity_ids[id(best)]
                    max_block=executor.w3.eth.block_number+max(1,int(os.getenv("DEX_MAX_BLOCKS_AHEAD","2")))
                    tx_hash=None
                    try:
                        tx_hash=await asyncio.to_thread(executor.build_and_send,best,max_block_number=max_block)
                        METRICS.increment("fresh_simulation_passed")
                        METRICS.mark(identifier, "simulation_passed", simulation_at=time.time())
                        METRICS.mark_submission(identifier, tx_hash)
                        receipt=await asyncio.to_thread(executor.wait_for_success,tx_hash,int(os.getenv("DEX_TX_RECEIPT_TIMEOUT","30")))
                        actual_profit = receipt.get("arb_profit_raw")
                        actual_profit_quote = None
                        realized_pnl_quote = None
                        if actual_profit is not None:
                            actual_profit_quote = Decimal(str(actual_profit)) / (Decimal(10) ** quote_decimals)
                            realized_pnl_quote = actual_profit_quote - best.gas_cost_quote
                            receipt["arb_profit_quote"] = str(actual_profit_quote)
                        METRICS.mark_included(identifier, receipt, realized_pnl_quote=realized_pnl_quote)
                        with LOCK: STATE["last_tx_hash"]=tx_hash
                        logging.info("ATOMIC FLASH TX CONFIRMED hash=%s gas_used=%s realized_pnl_quote=%s",tx_hash,receipt.get("gasUsed"),realized_pnl_quote)
                        await asyncio.sleep(float(os.getenv("DEX_LIVE_COOLDOWN_SECONDS","1.0")))
                    except FlashTransactionReverted as exc:
                        METRICS.mark_reverted(identifier, error=str(exc), tx_hash=exc.tx_hash, receipt=exc.receipt)
                        raise
                    except Exception as exc:
                        METRICS.mark(identifier, "simulation_failed" if tx_hash is None else "inclusion_failed", error=str(exc), tx_hash=tx_hash)
                        raise
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
