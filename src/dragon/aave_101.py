from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class AaveAction(str, Enum):
    SUPPLY = "supply"
    BORROW = "borrow"
    WITHDRAW = "withdraw"
    REPAY = "repay"
    FLASH_LOAN = "flash_loan"


@dataclass(frozen=True)
class AaveReserveState:
    frozen: bool = False
    paused: bool = False
    available_liquidity: Decimal | None = None


@dataclass(frozen=True)
class AavePositionState:
    health_factor: Decimal | None = None
    collateral_enabled: bool = False


class Aave101Model:
    """Minimal protocol model used to keep lending semantics separate from Dragon's DEX/flash path.

    Reserve flags follow Aave MCP safety semantics. Flash loans are classified as
    atomic liquidity operations, not as collateralized user lending positions.
    """

    @staticmethod
    def can_execute_reserve_action(state: AaveReserveState, action: AaveAction) -> bool:
        if action == AaveAction.FLASH_LOAN:
            return True
        if state.paused:
            return False
        if state.frozen and action in {AaveAction.SUPPLY, AaveAction.BORROW}:
            return False
        return True

    @staticmethod
    def has_borrowing_power(position: AavePositionState) -> bool:
        return bool(position.collateral_enabled and position.health_factor is not None and position.health_factor > 0)

    @staticmethod
    def is_liquidatable(position: AavePositionState) -> bool:
        return bool(position.health_factor is not None and position.health_factor < Decimal("1"))

    @staticmethod
    def context(action: AaveAction) -> str:
        if action == AaveAction.FLASH_LOAN:
            return "atomic_liquidity"
        if action in {AaveAction.SUPPLY, AaveAction.BORROW, AaveAction.WITHDRAW, AaveAction.REPAY}:
            return "collateralized_lending"
        return "unknown"
