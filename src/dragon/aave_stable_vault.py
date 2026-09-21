from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any


class StableVaultConfigError(ValueError):
    pass


def _decimal(value: Any, *, name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise StableVaultConfigError(f"{name} must be a finite decimal") from exc
    if not parsed.is_finite():
        raise StableVaultConfigError(f"{name} must be finite")
    return parsed


def _address(value: Any, *, name: str) -> str:
    text = str(value or "").strip()
    if len(text) != 42 or not text.startswith("0x"):
        raise StableVaultConfigError(f"{name} must be a 20-byte EVM address")
    try:
        int(text[2:], 16)
    except ValueError as exc:
        raise StableVaultConfigError(f"{name} contains non-hex characters") from exc
    return text


def _chain_ids(values: Any, *, name: str) -> tuple[int, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [x.strip() for x in values.split(",") if x.strip()]
    if not isinstance(values, (list, tuple)):
        raise StableVaultConfigError(f"{name} must be a list of chain ids")
    result: list[int] = []
    for value in values:
        try:
            chain_id = int(value)
        except (TypeError, ValueError) as exc:
            raise StableVaultConfigError(f"{name} contains an invalid chain id: {value!r}") from exc
        if chain_id <= 0:
            raise StableVaultConfigError(f"{name} contains non-positive chain id: {chain_id}")
        if chain_id not in result:
            result.append(chain_id)
    return tuple(result)


def _symbols(values: Any, *, name: str) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = [x.strip() for x in values.split(",") if x.strip()]
    if not isinstance(values, (list, tuple)):
        raise StableVaultConfigError(f"{name} must be a list")
    result: list[str] = []
    for value in values:
        symbol = str(value).strip().upper()
        if symbol and symbol not in result:
            result.append(symbol)
    return tuple(result)


@dataclass(frozen=True)
class StableVaultConfig:
    """Configuration boundary for an Aave Stable Vault deployment.

    Stable Vaults separate an Accounting Chain from one or more Earning Chains,
    support multiple whitelisted stablecoins, and allocate capital through
    approved ERC-4626-compatible strategies. Dragon keeps these controls
    explicit and does not execute cross-chain vault operations implicitly.
    """

    name: str
    vault_address: str
    accounting_chain_id: int
    earning_chain_ids: tuple[int, ...]
    supported_stablecoins: tuple[str, ...]
    strategy_names: tuple[str, ...] = ()
    user_rate_apr_pct: Decimal | None = None
    allowlist_enabled: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _address(self.vault_address, name=f"{self.name}.vault_address")
        if self.accounting_chain_id <= 0:
            raise StableVaultConfigError(f"{self.name}.accounting_chain_id must be positive")
        if self.accounting_chain_id in self.earning_chain_ids:
            raise StableVaultConfigError(
                f"{self.name}: accounting chain must not also be listed as an earning chain"
            )
        if not self.supported_stablecoins:
            raise StableVaultConfigError(f"{self.name}: at least one supported stablecoin is required")
        for chain_id in self.earning_chain_ids:
            if chain_id <= 0:
                raise StableVaultConfigError(f"{self.name}: invalid earning chain id {chain_id}")
        if self.user_rate_apr_pct is not None and self.user_rate_apr_pct < 0:
            raise StableVaultConfigError(f"{self.name}: user_rate_apr_pct cannot be negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "vault_address": self.vault_address,
            "accounting_chain_id": self.accounting_chain_id,
            "earning_chain_ids": list(self.earning_chain_ids),
            "supported_stablecoins": list(self.supported_stablecoins),
            "strategy_names": list(self.strategy_names),
            "user_rate_apr_pct": (
                str(self.user_rate_apr_pct) if self.user_rate_apr_pct is not None else None
            ),
            "allowlist_enabled": self.allowlist_enabled,
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class StableVaultSnapshot:
    config: StableVaultConfig
    healthy: bool
    issues: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload = self.config.as_dict()
        payload.update(
            {
                "healthy": self.healthy,
                "issues": list(self.issues),
                "architecture": {
                    "accounting_chain": self.config.accounting_chain_id,
                    "earning_chains": list(self.config.earning_chain_ids),
                    "multi_stablecoin": len(self.config.supported_stablecoins) > 1,
                    "strategy_count": len(self.config.strategy_names),
                    "allowlist_enabled": self.config.allowlist_enabled,
                },
            }
        )
        return payload


def parse_stable_vault_config(payload: dict[str, Any]) -> StableVaultConfig:
    if not isinstance(payload, dict):
        raise StableVaultConfigError("stable vault config must be an object")
    return StableVaultConfig(
        name=str(payload.get("name") or "").strip() or "stable-vault",
        vault_address=_address(payload.get("vault_address"), name="vault_address"),
        accounting_chain_id=int(payload.get("accounting_chain_id")),
        earning_chain_ids=_chain_ids(payload.get("earning_chain_ids"), name="earning_chain_ids"),
        supported_stablecoins=_symbols(
            payload.get("supported_stablecoins"), name="supported_stablecoins"
        ),
        strategy_names=_symbols(payload.get("strategy_names"), name="strategy_names"),
        user_rate_apr_pct=(
            _decimal(payload["user_rate_apr_pct"], name="user_rate_apr_pct")
            if payload.get("user_rate_apr_pct") is not None
            else None
        ),
        allowlist_enabled=bool(payload.get("allowlist_enabled", False)),
        enabled=bool(payload.get("enabled", True)),
        metadata=dict(payload.get("metadata") or {}),
    )


def load_stable_vaults_from_env(
    env_name: str = "AAVE_STABLE_VAULTS_JSON",
) -> tuple[StableVaultConfig, ...]:
    raw = os.getenv(env_name, "").strip()
    if not raw:
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StableVaultConfigError(f"{env_name} is not valid JSON") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise StableVaultConfigError(f"{env_name} must contain an object or list of objects")
    configs = tuple(parse_stable_vault_config(item) for item in payload)
    names: set[str] = set()
    for config in configs:
        if config.name in names:
            raise StableVaultConfigError(f"duplicate Stable Vault name: {config.name}")
        names.add(config.name)
    return configs


def validate_stable_vault(config: StableVaultConfig) -> StableVaultSnapshot:
    issues: list[str] = []
    if not config.enabled:
        issues.append("disabled")
    if not config.earning_chain_ids:
        issues.append("no earning chains configured")
    if not config.strategy_names:
        issues.append("no ERC-4626 strategies configured")
    if config.allowlist_enabled and not config.metadata.get("allowlist"):
        issues.append("allowlist enabled but no allowlist metadata configured")
    return StableVaultSnapshot(config=config, healthy=not issues, issues=tuple(issues))


def _bytes(value: str | bytes, *, name: str) -> bytes:
    if isinstance(value, bytes):
        return value
    text = str(value or "").strip()
    if not text:
        return b""
    if text.startswith("0x"):
        text = text[2:]
    if len(text) % 2:
        raise StableVaultConfigError(f"{name} hex data must have an even length")
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise StableVaultConfigError(f"{name} must be hex bytes") from exc


def _uint(value: int | str, *, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise StableVaultConfigError(f"{name} must be an integer") from exc
    if parsed < 0:
        raise StableVaultConfigError(f"{name} cannot be negative")
    return parsed


def _encode_call(signature: str, types: list[str], values: list[Any]) -> str:
    """Encode a Stable Vault call without connecting to an RPC or wallet."""
    from eth_abi import encode
    from web3 import Web3

    selector = Web3.keccak(text=signature)[:4]
    return "0x" + (selector + encode(types, values)).hex()


def _unsigned_tx(*, chain_id: int, vault: str, data: str) -> dict[str, Any]:
    return {
        "to": _address(vault, name="vault"),
        "data": data,
        "value": "0",
        "chainId": int(chain_id),
    }


def build_deposit_tx(
    *,
    chain_id: int,
    vault: str,
    user: str,
    asset: str,
    amount: int | str,
    policy_data: str | bytes = b"",
) -> dict[str, Any]:
    """Build an unsigned Stable Vault deposit transaction.

    The ERC-20 allowance and the caller's wallet signature remain explicit.
    """
    return _unsigned_tx(
        chain_id=chain_id,
        vault=vault,
        data=_encode_call(
            "deposit(address,address,uint256,bytes)",
            ["address", "address", "uint256", "bytes"],
            [
                _address(user, name="user"),
                _address(asset, name="asset"),
                _uint(amount, name="amount"),
                _bytes(policy_data, name="policy_data"),
            ],
        ),
    )


def build_request_withdrawal_tx(
    *,
    chain_id: int,
    vault: str,
    user: str,
    requested_amount_ray: int | str,
    policy_data: str | bytes = b"",
) -> dict[str, Any]:
    """Build an unsigned Stable Vault withdrawal-request transaction."""
    return _unsigned_tx(
        chain_id=chain_id,
        vault=vault,
        data=_encode_call(
            "requestWithdrawal(address,uint256,bytes)",
            ["address", "uint256", "bytes"],
            [
                _address(user, name="user"),
                _uint(requested_amount_ray, name="requested_amount_ray"),
                _bytes(policy_data, name="policy_data"),
            ],
        ),
    )


def build_execute_withdrawal_tx(
    *,
    chain_id: int,
    vault: str,
    user: str,
    asset_out: str,
    min_amount_out: int | str,
    iou_amount_ray: int | str,
    policy_data: str | bytes = b"",
) -> dict[str, Any]:
    """Build an unsigned Stable Vault withdrawal-execution transaction."""
    return _unsigned_tx(
        chain_id=chain_id,
        vault=vault,
        data=_encode_call(
            "executeWithdrawal(address,address,uint256,uint256,bytes)",
            ["address", "address", "uint256", "uint256", "bytes"],
            [
                _address(user, name="user"),
                _address(asset_out, name="asset_out"),
                _uint(min_amount_out, name="min_amount_out"),
                _uint(iou_amount_ray, name="iou_amount_ray"),
                _bytes(policy_data, name="policy_data"),
            ],
        ),
    )


def load_validated_stable_vaults() -> list[dict[str, Any]]:
    return [validate_stable_vault(config).as_dict() for config in load_stable_vaults_from_env()]
