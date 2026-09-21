from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable


def _dec(value: Any, default: Decimal = Decimal("0")) -> Decimal:
    if value is None:
        return default
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return parsed if parsed.is_finite() else default


def _nested_dec(obj: Any, *path: str) -> Decimal:
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return Decimal("0")
        current = current.get(key)
    return _dec(current)


@dataclass(frozen=True)
class AaveV4LiquidityCandidate:
    chain_id: int
    reserve_id: str
    on_chain_id: str
    spoke: str
    hub: str
    asset_id: str
    symbol: str
    token: str
    decimals: int
    active: bool
    frozen: bool
    paused: bool
    borrowable_flag: bool
    can_borrow: bool
    can_supply: bool
    supplied: Decimal
    borrowed: Decimal
    supply_cap: Decimal
    borrow_cap: Decimal
    suppliable: Decimal
    borrowable_amount: Decimal
    hub_liquidity: Decimal
    spoke_credit_line: Decimal
    spoke_credit_used: Decimal
    liquidity_ceiling: Decimal
    execution_ready: bool
    readiness_reason: str
    freshness_score: Decimal = Decimal("1")

    @property
    def usable(self) -> bool:
        return (
            self.active
            and not self.frozen
            and not self.paused
            and self.can_supply
            and self.liquidity_ceiling > 0
            and self.execution_ready
        )

    @property
    def score(self) -> Decimal:
        if not self.usable:
            return Decimal("-1")
        # Prefer actual executable headroom while mildly preferring fresh data.
        logish = min(self.liquidity_ceiling, Decimal("1000000"))
        return logish.ln() + (self.freshness_score * Decimal("0.25"))

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "reserve_id": self.reserve_id,
            "on_chain_id": self.on_chain_id,
            "spoke": self.spoke,
            "hub": self.hub,
            "asset_id": self.asset_id,
            "symbol": self.symbol,
            "token": self.token,
            "decimals": self.decimals,
            "active": self.active,
            "frozen": self.frozen,
            "paused": self.paused,
            "borrowable": self.borrowable_flag,
            "can_borrow": self.can_borrow,
            "can_supply": self.can_supply,
            "supplied": str(self.supplied),
            "borrowed": str(self.borrowed),
            "supply_cap": str(self.supply_cap),
            "borrow_cap": str(self.borrow_cap),
            "suppliable": str(self.suppliable),
            "borrowable_amount": str(self.borrowable_amount),
            "hub_liquidity": str(self.hub_liquidity),
            "spoke_credit_line": str(self.spoke_credit_line),
            "spoke_credit_used": str(self.spoke_credit_used),
            "liquidity_ceiling": str(self.liquidity_ceiling),
            "execution_ready": self.execution_ready,
            "readiness_reason": self.readiness_reason,
            "freshness_score": str(self.freshness_score),
            "score": str(self.score),
        }


def _hub_for_spoke(spoke_row: dict[str, Any], hub_address: str) -> dict[str, Any] | None:
    for row in spoke_row.get("connectedHubs", []) or []:
        hub = row.get("hub") if isinstance(row, dict) else None
        if not isinstance(hub, dict):
            continue
        if str(hub.get("address", "")).lower() == hub_address.lower():
            return row
    return None


def build_v4_candidates(
    *,
    spokes: Iterable[dict[str, Any]],
    reserves: Iterable[dict[str, Any]],
    chain_id: int,
    max_age_seconds: Decimal = Decimal("120"),
    age_seconds: Decimal | None = None,
) -> list[AaveV4LiquidityCandidate]:
    """Fuse AaveKit Spoke/Reserve data into executable liquidity candidates.

    This is deliberately a decision-data layer. It never assumes that a V4 Hub
    is a flash-loan pool and never signs/broadcasts anything.
    """
    freshness = Decimal("1")
    if age_seconds is not None:
        age = max(Decimal("0"), _dec(age_seconds))
        freshness = max(Decimal("0"), Decimal("1") - (age / max_age_seconds))

    spoke_by_address = {
        str(row.get("address", "")).lower(): row
        for row in spokes
        if isinstance(row, dict) and row.get("address")
    }

    out: list[AaveV4LiquidityCandidate] = []
    for reserve in reserves:
        if not isinstance(reserve, dict):
            continue

        token = reserve.get("asset", {}).get("token", {}) if isinstance(reserve.get("asset"), dict) else {}
        spoke = reserve.get("spoke", {}) if isinstance(reserve.get("spoke"), dict) else {}
        settings = reserve.get("settings", {}) if isinstance(reserve.get("settings"), dict) else {}
        status = reserve.get("status", {}) if isinstance(reserve.get("status"), dict) else {}
        summary = reserve.get("summary", {}) if isinstance(reserve.get("summary"), dict) else {}

        spoke_address = str(spoke.get("address", "")).strip()
        spoke_row = spoke_by_address.get(spoke_address.lower(), {})
        hub_address = str((spoke_row.get("connectedHubs") or [{}])[0].get("hub", {}).get("address", "")).strip()

        connected = _hub_for_spoke(spoke_row, hub_address) if hub_address else None
        hub_summary = connected.get("summary", {}) if isinstance(connected, dict) else {}

        supplied = _nested_dec(summary, "supplied", "amount", "value")
        borrowed = _nested_dec(summary, "borrowed", "amount", "value")
        supply_cap = _nested_dec(settings, "supplyCap", "amount", "value")
        borrow_cap = _nested_dec(settings, "borrowCap", "amount", "value")
        suppliable = _nested_dec(settings, "supplyCap", "amount", "value") - supplied
        borrowable_amount = _nested_dec(hub_summary, "creditLine", "value") - _nested_dec(hub_summary, "creditUsed", "value")

        hub_liquidity = _nested_dec(hub_summary, "creditLine", "value")
        credit_used = _nested_dec(hub_summary, "creditUsed", "value")
        credit_headroom = max(Decimal("0"), hub_liquidity - credit_used)

        if supply_cap > 0:
            reserve_headroom = max(Decimal("0"), supply_cap - supplied)
        else:
            reserve_headroom = Decimal("0")

        if hub_liquidity > 0:
            liquidity_ceiling = min(hub_liquidity, credit_headroom if credit_headroom > 0 else hub_liquidity)
        else:
            liquidity_ceiling = reserve_headroom

        if reserve_headroom > 0:
            liquidity_ceiling = min(liquidity_ceiling, reserve_headroom) if liquidity_ceiling > 0 else reserve_headroom

        active = bool(status.get("active", False))
        frozen = bool(status.get("frozen", False))
        paused = bool(status.get("paused", False))
        can_supply = bool(reserve.get("canSupply", False))
        can_borrow = bool(reserve.get("canBorrow", False))
        borrowable_flag = bool(settings.get("borrowable", False))

        reason = "ready"
        execution_ready = True
        if not active:
            execution_ready, reason = False, "reserve_inactive"
        elif frozen:
            execution_ready, reason = False, "reserve_frozen"
        elif paused:
            execution_ready, reason = False, "reserve_paused"
        elif not can_supply:
            execution_ready, reason = False, "reserve_not_suppliable"
        elif liquidity_ceiling <= 0:
            execution_ready, reason = False, "no_liquidity_headroom"
        elif age_seconds is not None and age_seconds > max_age_seconds:
            execution_ready, reason = False, "stale_data"

        try:
            decimals = int(token.get("decimals", 0))
        except (TypeError, ValueError):
            decimals = 0

        out.append(
            AaveV4LiquidityCandidate(
                chain_id=int(chain_id),
                reserve_id=str(reserve.get("id", "")),
                on_chain_id=str(reserve.get("onChainId", "")),
                spoke=spoke_address,
                hub=hub_address,
                asset_id=str(reserve.get("asset", {}).get("id", "")),
                symbol=str(token.get("symbol", "")),
                token=str(token.get("address", "")),
                decimals=decimals,
                active=active,
                frozen=frozen,
                paused=paused,
                borrowable_flag=borrowable_flag,
                can_borrow=can_borrow,
                can_supply=can_supply,
                supplied=supplied,
                borrowed=borrowed,
                supply_cap=supply_cap,
                borrow_cap=borrow_cap,
                suppliable=max(Decimal("0"), suppliable),
                borrowable_amount=max(Decimal("0"), borrowable_amount),
                hub_liquidity=max(Decimal("0"), hub_liquidity),
                spoke_credit_line=max(Decimal("0"), hub_liquidity),
                spoke_credit_used=max(Decimal("0"), credit_used),
                liquidity_ceiling=max(Decimal("0"), liquidity_ceiling),
                execution_ready=execution_ready,
                readiness_reason=reason,
                freshness_score=freshness,
            )
        )

    return sorted(out, key=lambda row: row.score, reverse=True)


def best_quote_liquidity(
    candidates: Iterable[AaveV4LiquidityCandidate],
    *,
    token: str,
    quote_decimals: int,
) -> Decimal:
    token_l = token.lower()
    matches = [c for c in candidates if c.usable and c.token.lower() == token_l]
    if not matches:
        return Decimal("0")
    return max(c.liquidity_ceiling for c in matches)


def summarize(candidates: Iterable[AaveV4LiquidityCandidate]) -> dict[str, Any]:
    rows = list(candidates)
    usable = [row for row in rows if row.usable]
    return {
        "candidates": len(rows),
        "usable": len(usable),
        "symbols": sorted({row.symbol for row in usable if row.symbol}),
        "max_liquidity": str(max((row.liquidity_ceiling for row in usable), default=Decimal("0"))),
        "top": [row.as_dict() for row in usable[:10]],
    }
