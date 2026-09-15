from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Config:
    api_base: str = "https://api.binance.com"
    ws_base: str = "wss://stream.binance.com:9443/ws"
    dry_run: bool = True
    live_trading: bool = False
    min_net_edge_bps: float = 20.0
    max_notional_usdt: float = 50.0
    max_slippage_bps: float = 10.0
    fee_bps: float = 10.0
    risk_pct: float = 0.01
    cooldown_ms: int = 3000
    max_triangles: int = 5000
    stale_ms: int = 2000
    poll_interval_seconds: float = 1.0

    @staticmethod
    def _bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_base=os.getenv("BINANCE_API_BASE", "https://api.binance.com").strip().rstrip("/"),
            ws_base=os.getenv("BINANCE_WS_BASE", "wss://stream.binance.com:9443/ws").strip().rstrip("/"),
            dry_run=cls._bool("DRY_RUN", True),
            live_trading=cls._bool("LIVE_TRADING", False),
            min_net_edge_bps=float(os.getenv("MIN_NET_EDGE_BPS", "20")),
            max_notional_usdt=float(os.getenv("MAX_NOTIONAL_USDT", "50")),
            max_slippage_bps=float(os.getenv("MAX_SLIPPAGE_BPS", "10")),
            fee_bps=float(os.getenv("FEE_BPS", "10")),
            risk_pct=float(os.getenv("ARB_RISK_PCT", "0.01")),
            cooldown_ms=int(os.getenv("LIVE_ORDER_COOLDOWN_MS", "3000")),
            max_triangles=int(os.getenv("MAX_TRIANGLES", "5000")),
            stale_ms=int(os.getenv("STALE_MS", "2000")),
            poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1")),
        )

    def validate(self) -> None:
        if not self.api_base.startswith(("https://", "http://")):
            raise ValueError("BINANCE_API_BASE must be an HTTP(S) URL")
        if not self.ws_base.startswith(("wss://", "ws://")):
            raise ValueError("BINANCE_WS_BASE must be a WS(S) URL")
        if self.max_notional_usdt <= 0 or self.risk_pct <= 0:
            raise ValueError("max notional and risk percentage must be positive")
        if self.min_net_edge_bps < 0 or self.fee_bps < 0 or self.max_slippage_bps < 0:
            raise ValueError("edge, fee, and slippage limits cannot be negative")
        if self.live_trading and self.dry_run:
            raise ValueError("LIVE_TRADING=true cannot be combined with DRY_RUN=true")
