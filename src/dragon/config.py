from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Config:
    dry_run: bool = True
    live_trading: bool = False
    min_net_edge_bps: float = 20.0
    max_notional_usdt: float = 50.0
    max_slippage_bps: float = 10.0
    fee_bps: float = 10.0
    poll_interval_seconds: float = 1.0

    @staticmethod
    def _bool(name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls) -> "Config":
        return cls(dry_run=cls._bool("DRY_RUN", True), live_trading=cls._bool("LIVE_TRADING", False), min_net_edge_bps=float(os.getenv("MIN_NET_EDGE_BPS", "20")), max_notional_usdt=float(os.getenv("MAX_NOTIONAL_USDT", "50")), max_slippage_bps=float(os.getenv("MAX_SLIPPAGE_BPS", "10")), fee_bps=float(os.getenv("FEE_BPS", "10")), poll_interval_seconds=float(os.getenv("POLL_INTERVAL_SECONDS", "1")))

    def validate(self) -> None:
        if self.max_notional_usdt <= 0:
            raise ValueError("MAX_NOTIONAL_USDT must be positive")
        if self.min_net_edge_bps < 0 or self.fee_bps < 0 or self.max_slippage_bps < 0:
            raise ValueError("edge, fee, and slippage limits cannot be negative")
        if self.live_trading and self.dry_run:
            raise ValueError("LIVE_TRADING=true cannot be combined with DRY_RUN=true")
