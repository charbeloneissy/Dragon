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
    """Keep Aave lending semantics separate from Dragon's atomic liquidity path.

    The reserve-flag rules here intentionally cover only the lending actions
    documented by Aave MCP. Flash-loan eligibility is handled by the live
    Pool/flash-loan adapter instead of being inferred from lending flags.
    """

    @staticmethod
    def can_execute_lending_action(state: AaveReserveState, action: AaveAction) -> bool:
        if action == AaveAction.FLASH_LOAN:
            raise ValueError("flash-loan eligibility is not a lending-reserve rule")
        if action not in {
            AaveAction.SUPPLY,
            AaveAction.BORROW,
            AaveAction.WITHDRAW,
            AaveAction.REPAY,
        }:
            raise ValueError(f"unsupported lending action: {action}")
        if state.paused:
            return False
        if state.frozen and action in {AaveAction.SUPPLY, AaveAction.BORROW}:
            return False
        return True

    @staticmethod
    def has_borrowing_power(position: AavePositionState) -> bool:
        return bool(
            position.collateral_enabled
            and position.health_factor is not None
            and position.health_factor > 0
        )

    @staticmethod
    def is_liquidatable(position: AavePositionState) -> bool:
        return bool(
            position.health_factor is not None
            and position.health_factor < Decimal("1")
        )

    @staticmethod
    def context(action: AaveAction) -> str:
        if action == AaveAction.FLASH_LOAN:
            return "atomic_liquidity"
        if action in {
            AaveAction.SUPPLY,
            AaveAction.BORROW,
            AaveAction.WITHDRAW,
            AaveAction.REPAY,
        }:
            return "collateralized_lending"
        return "unknown"
