from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from web3 import Web3


UINT256_MAX = 2**256 - 1
VARIABLE_RATE_MODE = 2
HEALTH_FACTOR_ONE_WAD = 10**18

POOL_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "asset", "type": "address"}],
        "name": "getReserveVariableDebtToken",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
            {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
            {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
            {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
            {"internalType": "uint256", "name": "ltv", "type": "uint256"},
            {"internalType": "uint256", "name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserEMode",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "interestRateMode", "type": "uint256"},
            {"internalType": "uint16", "name": "referralCode", "type": "uint16"},
            {"internalType": "address", "name": "onBehalfOf", "type": "address"},
        ],
        "name": "borrow",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]

CREDIT_DELEGATION_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "delegatee", "type": "address"},
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
        ],
        "name": "approveDelegation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "fromUser", "type": "address"},
            {"internalType": "address", "name": "toUser", "type": "address"},
        ],
        "name": "borrowAllowance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "delegator", "type": "address"}],
        "name": "renounceDelegation",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


@dataclass(frozen=True)
class CreditDelegationSnapshot:
    pool: str
    asset: str
    variable_debt_token: str
    delegator: str
    delegatee: str
    allowance: int
    total_collateral_base: int
    total_debt_base: int
    available_borrows_base: int
    health_factor_wad: int
    emode_category: int

    @property
    def health_factor(self) -> Decimal:
        if self.health_factor_wad == UINT256_MAX:
            return Decimal("Infinity")
        return Decimal(self.health_factor_wad) / Decimal(HEALTH_FACTOR_ONE_WAD)

    @property
    def healthy(self) -> bool:
        return self.health_factor_wad >= HEALTH_FACTOR_ONE_WAD

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool": self.pool,
            "asset": self.asset,
            "variable_debt_token": self.variable_debt_token,
            "delegator": self.delegator,
            "delegatee": self.delegatee,
            "allowance": str(self.allowance),
            "total_collateral_base": str(self.total_collateral_base),
            "total_debt_base": str(self.total_debt_base),
            "available_borrows_base": str(self.available_borrows_base),
            "health_factor_wad": str(self.health_factor_wad),
            "health_factor": str(self.health_factor),
            "emode_category": self.emode_category,
            "healthy": self.healthy,
        }


def _checksum(value: str, name: str) -> str:
    if not Web3.is_address(value):
        raise ValueError(f"{name} must be a valid EVM address")
    return Web3.to_checksum_address(value)


def validate_credit_delegation_request(
    *,
    pool: str,
    asset: str,
    delegator: str,
    delegatee: str,
    amount: int,
    delegator_emode_category: int | None = None,
    borrow_asset_emode_category: int | None = None,
) -> None:
    _checksum(pool, "pool")
    _checksum(asset, "asset")
    _checksum(delegator, "delegator")
    _checksum(delegatee, "delegatee")
    if delegator.lower() == delegatee.lower():
        raise ValueError("delegatee must differ from delegator")
    if amount <= 0:
        raise ValueError("delegated amount must be positive")
    if delegator_emode_category is not None and delegator_emode_category < 0:
        raise ValueError("delegator_emode_category cannot be negative")
    if borrow_asset_emode_category is not None and borrow_asset_emode_category < 0:
        raise ValueError("borrow_asset_emode_category cannot be negative")

    # For a supplied eMode category, the borrow asset must match that category.
    # A value of 0 means eMode is disabled. Liquid/multi-category assets need
    # an upstream protocol simulation to establish eligibility.
    if (
        delegator_emode_category is not None
        and borrow_asset_emode_category is not None
        and delegator_emode_category != 0
        and delegator_emode_category != borrow_asset_emode_category
    ):
        raise ValueError(
            "borrow asset eMode category is incompatible with the delegator eMode category"
        )


class AaveV3CreditDelegationClient:
    """Read/prepare client for Aave V3 credit delegation.

    This client never signs or broadcasts. The supplier must sign the
    approveDelegation transaction, while the delegatee separately signs the
    Pool.borrow transaction.
    """

    def __init__(self, w3: Web3):
        self.w3 = w3

    def _pool(self, pool: str):
        return self.w3.eth.contract(
            address=_checksum(pool, "pool"),
            abi=POOL_ABI,
        )

    def _debt_token(self, token: str):
        return self.w3.eth.contract(
            address=_checksum(token, "variable_debt_token"),
            abi=CREDIT_DELEGATION_ABI,
        )

    def variable_debt_token(self, pool: str, asset: str) -> str:
        token = self._pool(pool).functions.getReserveVariableDebtToken(
            _checksum(asset, "asset")
        ).call()
        if not token or token == "0x0000000000000000000000000000000000000000":
            raise ValueError("Aave V3 reserve has no variable debt token")
        return Web3.to_checksum_address(token)

    def borrow_allowance(
        self,
        variable_debt_token: str,
        delegator: str,
        delegatee: str,
    ) -> int:
        return int(
            self._debt_token(variable_debt_token).functions.borrowAllowance(
                _checksum(delegator, "delegator"),
                _checksum(delegatee, "delegatee"),
            ).call()
        )

    def snapshot(
        self,
        *,
        pool: str,
        asset: str,
        delegator: str,
        delegatee: str,
    ) -> CreditDelegationSnapshot:
        pool_cs = _checksum(pool, "pool")
        asset_cs = _checksum(asset, "asset")
        delegator_cs = _checksum(delegator, "delegator")
        delegatee_cs = _checksum(delegatee, "delegatee")
        debt_token = self.variable_debt_token(pool_cs, asset_cs)
        allowance = self.borrow_allowance(debt_token, delegator_cs, delegatee_cs)
        data = self._pool(pool_cs).functions.getUserAccountData(delegator_cs).call()
        emode = int(self._pool(pool_cs).functions.getUserEMode(delegator_cs).call())

        return CreditDelegationSnapshot(
            pool=pool_cs,
            asset=asset_cs,
            variable_debt_token=debt_token,
            delegator=delegator_cs,
            delegatee=delegatee_cs,
            allowance=allowance,
            total_collateral_base=int(data[0]),
            total_debt_base=int(data[1]),
            available_borrows_base=int(data[2]),
            health_factor_wad=int(data[5]),
            emode_category=emode,
        )

    def build_approve_delegation(
        self,
        *,
        variable_debt_token: str,
        delegator: str,
        delegatee: str,
        amount: int,
        chain_id: int,
    ) -> dict[str, Any]:
        _checksum(variable_debt_token, "variable_debt_token")
        _checksum(delegator, "delegator")
        _checksum(delegatee, "delegatee")
        if amount <= 0:
            raise ValueError("delegated amount must be positive")
        if delegator.lower() == delegatee.lower():
            raise ValueError("delegatee must differ from delegator")
        tx = self._debt_token(variable_debt_token).functions.approveDelegation(
            _checksum(delegatee, "delegatee"),
            int(amount),
        ).build_transaction(
            {
                "from": _checksum(delegator, "delegator"),
                "chainId": int(chain_id),
            }
        )
        return tx

    def build_renounce_delegation(
        self,
        *,
        variable_debt_token: str,
        delegatee: str,
        delegator: str,
        chain_id: int,
    ) -> dict[str, Any]:
        tx = self._debt_token(variable_debt_token).functions.renounceDelegation(
            _checksum(delegator, "delegator")
        ).build_transaction(
            {
                "from": _checksum(delegatee, "delegatee"),
                "chainId": int(chain_id),
            }
        )
        return tx

    def build_borrow(
        self,
        *,
        pool: str,
        asset: str,
        amount: int,
        delegatee: str,
        delegator: str,
        chain_id: int,
        referral_code: int = 0,
    ) -> dict[str, Any]:
        validate_credit_delegation_request(
            pool=pool,
            asset=asset,
            delegator=delegator,
            delegatee=delegatee,
            amount=amount,
        )
        return self._pool(pool).functions.borrow(
            _checksum(asset, "asset"),
            int(amount),
            VARIABLE_RATE_MODE,
            int(referral_code),
            _checksum(delegator, "delegator"),
        ).build_transaction(
            {
                "from": _checksum(delegatee, "delegatee"),
                "chainId": int(chain_id),
            }
        )

    def preflight(
        self,
        snapshot: CreditDelegationSnapshot,
        *,
        requested_amount: int,
        borrow_asset_emode_category: int | None = None,
    ) -> dict[str, Any]:
        if requested_amount <= 0:
            raise ValueError("requested_amount must be positive")
        if snapshot.allowance < requested_amount:
            raise ValueError(
                f"delegated allowance {snapshot.allowance} is below requested amount {requested_amount}"
            )
        if not snapshot.healthy:
            raise ValueError(
                f"delegator health factor is below 1: {snapshot.health_factor}"
            )
        if snapshot.available_borrows_base <= 0:
            raise ValueError("delegator has no remaining borrowing power")
        if (
            snapshot.emode_category != 0
            and borrow_asset_emode_category is not None
            and snapshot.emode_category != borrow_asset_emode_category
        ):
            raise ValueError(
                "borrow asset is outside the delegator's active eMode category"
            )
        return {
            "allowed": True,
            "allowance_sufficient": True,
            "health_factor_sufficient": True,
            "borrowing_power_available": True,
            "emode_checked": borrow_asset_emode_category is not None,
            "note": "Final borrow transaction must still be simulated/protocol-validated before signing.",
        }
