from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Config:
    api_base: str = "https://api.binance.com"
    ws_base: str = "wss://stream.binance.com:9443/ws"
    dry_run: bool = True
    live_trading: bool = False
    min_net_edge_bps: float = 0.5
    max_notional_usdt: float = 25.0
    max_slippage_bps: float = 10.0
    fee_bps: float = 10.0
    risk_pct: float = 0.01
    cooldown_ms: int = 1000
    max_triangles: int = 5000
    stale_ms: int = 750
    poll_interval_seconds: float = 0.05
    order_timeout_ms: int = 5000
    depth_levels: int = 20
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
        return cls(
            api_base=os.getenv("BINANCE_API_BASE", cls.api_base).strip().rstrip("/"),
            ws_base=os.getenv("BINANCE_WS_BASE", cls.ws_base).strip().rstrip("/"),
            dry_run=cls._bool("DRY_RUN", cls.dry_run),
            live_trading=cls._bool("LIVE_TRADING", cls.live_trading),
            min_net_edge_bps=float(os.getenv("MIN_NET_EDGE_BPS", str(cls.min_net_edge_bps))),
            max_notional_usdt=float(os.getenv("MAX_NOTIONAL_USDT", str(cls.max_notional_usdt))),
            max_slippage_bps=float(os.getenv("MAX_SLIPPAGE_BPS", str(cls.max_slippage_bps))),
            fee_bps=float(os.getenv("FEE_BPS", str(cls.fee_bps))),
            risk_pct=float(os.getenv("ARB_RISK_PCT", str(cls.risk_pct))),
            cooldown_ms=int(os.getenv("LIVE_ORDER_COOLDOWN_MS", str(cls.cooldown_ms))),
            max_triangles=int(os.getenv("MAX_TRIANGLES", str(cls.max_triangles))),
            stale_ms=int(os.getenv("STALE_MS", str(cls.stale_ms))),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", str(cls.poll_interval_seconds))),
            order_timeout_ms=int(os.getenv("ORDER_TIMEOUT_MS", str(cls.order_timeout_ms))),
            depth_levels=int(os.getenv("DEPTH_LEVELS", str(cls.depth_levels))),
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
        if self.max_notional_usdt <= 0 or self.risk_pct <= 0:
            raise ValueError("max notional and risk percentage must be positive")
        if self.risk_pct > 0.01:
            raise ValueError("ARB_RISK_PCT cannot exceed 0.01 (1%)")
        if self.min_net_edge_bps < 0 or self.fee_bps < 0 or self.max_slippage_bps < 0:
            raise ValueError("edge, fee, and slippage limits cannot be negative")
        if self.cooldown_ms < 0 or self.stale_ms <= 0 or self.order_timeout_ms <= 0:
            raise ValueError("timing values are invalid")
        if not 1 <= self.depth_levels <= 100:
            raise ValueError("DEPTH_LEVELS must be between 1 and 100")
        if self.dex_max_gas_quote < 0 or self.dex_max_latency_ms <= 0:
            raise ValueError("DEX cost/latency limits are invalid")
        if self.dex_enabled and not self.dex_quote_url:
            raise ValueError("DEX_ENABLED=true requires DEX_QUOTE_URL")
        if self.live_trading and self.dry_run:
            raise ValueError("LIVE_TRADING=true cannot be combined with DRY_RUN=true")
