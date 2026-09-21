from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Mapping, Sequence


class BrainSource(str, Enum):
    ONCHAIN = "onchain"
    DEX_QUOTE = "dex_quote"
    AAVE = "aave"
    CELO_AI = "celo_ai"
    HISTORY = "history"


class RejectCode(str, Enum):
    STALE_STATE = "STALE_STATE"
    INVALID_QUOTE = "INVALID_QUOTE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    GAS_TOO_HIGH = "GAS_TOO_HIGH"
    SLIPPAGE_TOO_HIGH = "SLIPPAGE_TOO_HIGH"
    FLASH_COST_TOO_HIGH = "FLASH_COST_TOO_HIGH"
    NET_BELOW_TARGET = "NET_BELOW_TARGET"
    SIMULATION_FAILED = "SIMULATION_FAILED"
    INVALID_CALLDATA = "INVALID_CALLDATA"
    REPAYMENT_UNSAFE = "REPAYMENT_UNSAFE"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


@dataclass(frozen=True)
class IntelligenceFact:
    key: str
    value: Decimal | str | int | bool
    source: BrainSource
    observed_at_ms: int
    block_number: int | None = None
    confidence: Decimal = Decimal("1")
    expires_at_ms: int | None = None


@dataclass(frozen=True)
class MarketSnapshot:
    chain_id: int
    block_number: int
    observed_at_ms: int
    venue: str
    token_in: str
    token_out: str
    amount_in: Decimal
    amount_out: Decimal
    fee_bps: Decimal
    liquidity_quote: Decimal
    gas_quote: Decimal
    quote_age_ms: int
    facts: tuple[IntelligenceFact, ...] = ()


@dataclass(frozen=True)
class LiquidityState:
    chain_id: int
    asset: str
    flash_available: Decimal
    flash_fee_bps: Decimal
    route_capacity: Decimal
    dex_capacity: Decimal
    observed_at_ms: int
    block_number: int
    confidence: Decimal = Decimal("1")

    @property
    def executable_capacity(self) -> Decimal:
        return min(self.flash_available, self.route_capacity, self.dex_capacity)


@dataclass(frozen=True)
class Opportunity:
    opportunity_id: str
    chain_id: int
    venue_path: tuple[str, ...]
    token_path: tuple[str, ...]
    amount_in: Decimal
    expected_out: Decimal
    gross_profit: Decimal
    dex_fees: Decimal
    flash_fee: Decimal
    gas: Decimal
    slippage: Decimal
    price_impact: Decimal
    quote_age_ms: int
    execution_latency_ms: int
    execution_probability: Decimal
    state_stability: Decimal
    facts: tuple[IntelligenceFact, ...] = ()

    @property
    def deterministic_net(self) -> Decimal:
        return (
            self.gross_profit
            - self.dex_fees
            - self.flash_fee
            - self.gas
            - self.slippage
            - self.price_impact
        )

    @property
    def expected_realized_net(self) -> Decimal:
        return self.deterministic_net * self.execution_probability * self.state_stability


@dataclass(frozen=True)
class ExecutionPlan:
    plan_id: str
    opportunity_id: str
    chain_id: int
    flash_asset: str
    flash_amount: Decimal
    target: str
    value: Decimal
    calldata: bytes
    gas_limit: int
    min_profit: Decimal
    repayment_amount: Decimal
    created_block: int
    expires_at_ms: int


@dataclass(frozen=True)
class ExecutionResult:
    tx_hash: str
    success: bool
    block_number: int | None
    actual_input: Decimal
    actual_output: Decimal
    gas_used: int
    actual_gas_cost: Decimal
    repayment: Decimal
    realized_net: Decimal
    verified: bool


@dataclass(frozen=True)
class BrainState:
    chain_id: int
    block_number: int
    observed_at_ms: int
    facts: tuple[IntelligenceFact, ...] = field(default_factory=tuple)
    metrics: Mapping[str, Decimal] = field(default_factory=dict)
