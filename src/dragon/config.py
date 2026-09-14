from dataclasses import dataclass
import os


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    api_base: str = "https://api.binance.com"
    ws_base: str = "wss://stream.binance.com:9443/ws"
    dry_run: bool = True
    live_trading: bool = False
    min_net_edge_bps: float = 8.0
    fee_bps: float = 10.0
    slippage_bps: float = 3.0
    max_notional_usdt: float = 10.0
    risk_pct: float = 0.01
    cooldown_ms: int = 5000
    max_triangles: int = 3000
    stale_ms: int = 1500

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            api_base=os.getenv("BINANCE_API_BASE", "https://api.binance.com").rstrip("/"),
            ws_base=os.getenv("BINANCE_WS_BASE", "wss://stream.binance.com:9443/ws").rstrip("/"),
            dry_run=_bool("DRY_RUN", True),
            live_trading=_bool("LIVE_TRADING", False),
            min_net_edge_bps=float(os.getenv("MIN_NET_EDGE_BPS", "8")),
            fee_bps=float(os.getenv("FEE_BPS", "10")),
            slippage_bps=float(os.getenv("SLIPPAGE_BPS", "3")),
            max_notional_usdt=float(os.getenv("MAX_NOTIONAL_USDT", "10")),
            risk_pct=float(os.getenv("ARB_RISK_PCT", "0.01")),
            cooldown_ms=int(os.getenv("LIVE_ORDER_COOLDOWN_MS", "5000")),
            max_triangles=int(os.getenv("MAX_TRIANGLES", "3000")),
            stale_ms=int(os.getenv("STALE_MS", "1500")),
        )

    def validate(self) -> None:
        if self.live_trading and self.dry_run:
            raise ValueError("LIVE_TRADING=true cannot be combined with DRY_RUN=true")
        if self.max_notional_usdt <= 0 or self.risk_pct <= 0:
            raise ValueError("risk and max notional must be positive")
        if self.min_net_edge_bps < 0 or self.fee_bps < 0 or self.slippage_bps < 0:
            raise ValueError("thresholds cannot be negative")
