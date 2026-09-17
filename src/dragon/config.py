from dataclasses import dataclass
import os


# Legacy CEX configuration is retained for compatibility but has no owned-capital budget.
# The active DEX engine is flash-liquidity based and must not inherit a fixed starting balance.
FIXED_STARTING_CAPITAL_USDT = 0.0
FIXED_SAFETY_RESERVE_USDT = 0.0
FIXED_DEPLOYABLE_USDT = 0.0
FIXED_MIN_TRADE_NOTIONAL_USDT = 0.0
FIXED_MAX_NOTIONAL_USDT = 0.0
FIXED_DECISION_CYCLE_SECONDS = 30
FIXED_UNIVERSE_SYMBOL_CAP = 1000
FIXED_PROBABILITY_SAMPLE_SIZE = 1000
FIXED_ROLLING_WINDOWS = (100, 500, 1000)
FIXED_NET_EDGE_FLOOR_BPS = 2.0
FIXED_NET_EDGE_REFERENCE_BPS = 20.0
FIXED_SPOT_ONLY = True
FIXED_FUTURES_ENABLED = False
FIXED_LEVERAGE_ENABLED = False
FIXED_MARTINGALE_ENABLED = False
FIXED_ONE_TIME_COMPOUND_TARGET_USDT = 0.0


@dataclass(frozen=True)
class Config:
    api_base: str = "https://api.binance.com"
    ws_base: str = "wss://stream.binance.com:9443/ws"
    dry_run: bool = True
    live_trading: bool = False
    flash_loan_only: bool = True
    own_capital_usdt: float = 0.0
    flash_loan_liquidity_usdt: float = 0.0
    min_net_edge_bps: float = FIXED_NET_EDGE_FLOOR_BPS
    min_expected_profit_usdt: float = 0.005
    min_trade_notional_usdt: float = 0.0
    max_notional_usdt: float = 0.0
    max_slippage_bps: float = 15.0
    fee_bps: float = 10.0
    risk_pct: float = 0.0
    capital_allocation_pct: float = 0.0
    safety_reserve_usdt: float = 0.0
    cooldown_ms: int = 1000
    max_triangles: int = 0
    stale_ms: int = 1000
    poll_interval_seconds: float = 0.05
    order_timeout_ms: int = 5000
    depth_levels: int = 20
    decision_cycle_seconds: int = FIXED_DECISION_CYCLE_SECONDS
    universe_mode: str = "FULL_DYNAMIC"
    health_fail_open: bool = False
    dex_enabled: bool = False
    dex_quote_url: str = ""
    dex_max_gas_quote: float = 0.0
    dex_max_latency_ms: int = 2000

    @staticmethod
    def _bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls) -> "Config":
        requested_min_edge = float(os.getenv("MIN_NET_EDGE_BPS", str(cls.min_net_edge_bps)))
        return cls(
            api_base=os.getenv("BINANCE_API_BASE", cls.api_base).strip().rstrip("/"),
            ws_base=os.getenv("BINANCE_WS_BASE", cls.ws_base).strip().rstrip("/"),
            dry_run=cls._bool("DRY_RUN", cls.dry_run),
            live_trading=cls._bool("LIVE_TRADING", cls.live_trading),
            flash_loan_only=cls._bool("FLASH_LOAN_ONLY", cls.flash_loan_only),
            own_capital_usdt=0.0,
            flash_loan_liquidity_usdt=float(os.getenv("DEX_FLASH_LOAN_LIQUIDITY_QUOTE", "0")),
            min_net_edge_bps=max(FIXED_NET_EDGE_FLOOR_BPS, requested_min_edge),
            min_expected_profit_usdt=max(0.005, float(os.getenv("MIN_EXPECTED_PROFIT_USDT", "0.005"))),
            min_trade_notional_usdt=0.0,
            max_notional_usdt=0.0,
            max_slippage_bps=float(os.getenv("MAX_SLIPPAGE_BPS", str(cls.max_slippage_bps))),
            fee_bps=float(os.getenv("FEE_BPS", str(cls.fee_bps))),
            risk_pct=0.0,
            capital_allocation_pct=0.0,
            safety_reserve_usdt=0.0,
            cooldown_ms=int(os.getenv("LIVE_ORDER_COOLDOWN_MS", str(cls.cooldown_ms))),
            max_triangles=0,
            stale_ms=int(os.getenv("STALE_MS", str(cls.stale_ms))),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", str(cls.poll_interval_seconds))),
            order_timeout_ms=int(os.getenv("ORDER_TIMEOUT_MS", str(cls.order_timeout_ms))),
            depth_levels=int(os.getenv("DEPTH_LEVELS", str(cls.depth_levels))),
            decision_cycle_seconds=FIXED_DECISION_CYCLE_SECONDS,
            universe_mode="FULL_DYNAMIC",
            health_fail_open=cls._bool("HEALTH_FAIL_OPEN", cls.health_fail_open),
            dex_enabled=cls._bool("DEX_ENABLED", cls.dex_enabled),
            dex_quote_url=os.getenv("DEX_QUOTE_URL", cls.dex_quote_url).strip(),
            dex_max_gas_quote=float(os.getenv("DEX_MAX_GAS_QUOTE", str(cls.dex_max_gas_quote))),
            dex_max_latency_ms=int(os.getenv("DEX_MAX_LATENCY_MS", str(cls.dex_max_latency_ms))),
        )

    @property
    def slippage_bps(self) -> float:
        return self.max_slippage_bps

    def validate(self) -> None:
        if not self.api_base.startswith(("https://", "http://")):
            raise ValueError("BINANCE_API_BASE must be an HTTP(S) URL")
        if not self.ws_base.startswith(("wss://", "ws://")):
            raise ValueError("BINANCE_WS_BASE must be a WebSocket URL")
        if self.own_capital_usdt != 0:
            raise ValueError("owned capital must remain exactly 0")
        if not self.flash_loan_only:
            raise ValueError("Dragon must use flash-loan-only capital mode")
        if self.min_expected_profit_usdt < 0.005:
            raise ValueError("minimum net profit cannot be below 0.005")
        if self.min_net_edge_bps < FIXED_NET_EDGE_FLOOR_BPS:
            raise ValueError("MIN_NET_EDGE_BPS cannot be tuned below the fixed 2 bps floor")
        if self.min_trade_notional_usdt != 0 or self.max_notional_usdt != 0:
            raise ValueError("owned-capital trade quotas are disabled in flash-loan-only mode")
        if self.safety_reserve_usdt != 0 or self.capital_allocation_pct != 0:
            raise ValueError("owned-capital reserve/allocation must remain zero")
        if self.max_slippage_bps < 0 or self.fee_bps < 0:
            raise ValueError("fee and slippage limits cannot be negative")
        if self.cooldown_ms < 0 or self.stale_ms <= 0 or self.order_timeout_ms <= 0:
            raise ValueError("timing values are invalid")
        if not 1 <= self.depth_levels <= 100:
            raise ValueError("DEPTH_LEVELS must be between 1 and 100")
        if self.decision_cycle_seconds <= 0:
            raise ValueError("DECISION_CYCLE_SECONDS must be positive")
        if self.universe_mode != "FULL_DYNAMIC":
            raise ValueError("UNIVERSE_MODE must be FULL_DYNAMIC")
        if self.dex_max_gas_quote < 0 or self.dex_max_latency_ms <= 0:
            raise ValueError("DEX cost/latency limits are invalid")
        if self.dex_enabled and not self.dex_quote_url:
            raise ValueError("DEX_ENABLED=true requires DEX_QUOTE_URL")
        if self.live_trading:
            raise ValueError("live execution remains disabled until the flash-loan MEV-protected atomic executor exists")
