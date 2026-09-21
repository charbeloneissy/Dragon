from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

ERROR_LEVEL = "error"
WARNING_LEVEL = "warning"
INFO_LEVEL = "info"

# Snapshot of the official Aave skills repository used to align Dragon policy.
AAVE_SKILLS_COMMIT = "b21a0345f47f5fb8337d6769f927b7b56ff3943a"


@dataclass(frozen=True)
class AaveAgentDecision:
    allowed: bool
    phase: str
    reason: str
    warnings: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "phase": self.phase,
            "reason": self.reason,
            "warnings": list(self.warnings),
        }


class AaveAgentPolicy:
    """Dragon-side policy aligned with Aave official MCP lifecycle.

    discover -> inspect -> simulate -> build -> wallet-sign -> confirm

    This policy never signs, broadcasts, or creates custody.
    """

    def __init__(self, *, require_simulation: bool = True, require_confirmation: bool = True):
        self.require_simulation = bool(require_simulation)
        self.require_confirmation = bool(require_confirmation)

    @staticmethod
    def warnings(result: Any) -> tuple[dict[str, Any], ...]:
        if not isinstance(result, dict):
            return ()
        raw = result.get("warnings")
        if not isinstance(raw, list):
            return ()
        return tuple(
            item
            for item in raw
            if isinstance(item, dict)
            and str(item.get("level", "")).lower()
            in {ERROR_LEVEL, WARNING_LEVEL, INFO_LEVEL}
        )

    @staticmethod
    def coverage(
        result: Mapping[str, Any] | None,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...], tuple[Any, ...]]:
        """Return chainsCovered / chainsNotCovered / chainsNotServed from an Aave response."""
        if not isinstance(result, Mapping):
            return (), (), ()
        return (
            tuple(result.get("chainsCovered") or ()),
            tuple(result.get("chainsNotCovered") or ()),
            tuple(result.get("chainsNotServed") or ()),
        )

    def guard(
        self,
        *,
        phase: str,
        result: Any = None,
        chains_covered: Iterable[Any] = (),
        chains_not_served: Iterable[Any] = (),
        chains_not_covered: Iterable[Any] = (),
    ) -> AaveAgentDecision:
        warnings = self.warnings(result)
        result_covered, result_not_covered, result_not_served = self.coverage(
            result if isinstance(result, Mapping) else None
        )
        covered = tuple(chains_covered) or result_covered
        not_covered = tuple(chains_not_covered) or result_not_covered
        not_served = tuple(chains_not_served) or result_not_served

        for warning in warnings:
            if str(warning.get("level", "")).lower() == ERROR_LEVEL:
                return AaveAgentDecision(
                    allowed=False,
                    phase=phase,
                    reason=f"error_warning:{warning.get('code', 'unknown')}",
                    warnings=warnings,
                )

        if not_served:
            return AaveAgentDecision(
                allowed=False,
                phase=phase,
                reason="requested_chain_not_served",
                warnings=warnings,
            )

        if not_covered:
            return AaveAgentDecision(
                allowed=False,
                phase=phase,
                reason="required_chain_not_covered",
                warnings=warnings,
            )

        if phase == 'discover' and not covered and isinstance(result, Mapping):
            return AaveAgentDecision(
                allowed=False,
                phase=phase,
                reason="no_coverage_reported",
                warnings=warnings,
            )

        if phase in {"borrow", "withdraw", "repay", "supply"} and self.require_simulation:
            if not isinstance(result, Mapping) or not result.get("_dragon_simulation_ok", False):
                return AaveAgentDecision(
                    allowed=False,
                    phase=phase,
                    reason="simulation_required",
                    warnings=warnings,
                )

        if phase == 'build':
            return AaveAgentDecision(
                allowed=True,
                phase=phase,
                reason="unsigned_transaction_ready",
                warnings=warnings,
            )

        if phase == 'wallet-sign':
            return AaveAgentDecision(
                allowed=True,
                phase=phase,
                reason="external_wallet_required",
                warnings=warnings,
            )

        if phase == 'confirm' and self.require_confirmation:
            return AaveAgentDecision(
                allowed=True,
                phase=phase,
                reason="await_protocol_confirmation",
                warnings=warnings,
            )

        return AaveAgentDecision(
            allowed=True,
            phase=phase,
            reason="policy_ok",
            warnings=warnings,
        )

    @staticmethod
    def mark_simulation(result: dict[str, Any], *, ok: bool = True) -> dict[str, Any]:
        enriched = dict(result)
        enriched["_dragon_simulation_ok"] = bool(ok)
        return enriched

    def execution_plan_summary(self, plan: Any) -> dict[str, Any]:
        """Interpret an Aave execution plan without ever signing it."""
        if not isinstance(plan, dict):
            return {"type": type(plan).__name__, "ready": False}

        typename = str(plan.get("__typename") or plan.get("operation") or "unknown")
        summary = {"type": typename, "ready": False, "requires_wallet_signature": False}

        if typename == "TransactionRequest":
            required = ("to", "from", "data", "value", "chainId")
            missing = [key for key in required if key not in plan]
            summary.update({
                "ready": not missing,
                "missing_fields": missing,
                "chainId": plan.get("chainId"),
                "to": plan.get("to"),
                "from": plan.get("from"),
                "operation": plan.get("operation"),
                "requires_wallet_signature": not missing,
            })

        elif typename in {"ApprovalRequired", "Erc20ApprovalRequired"}:
            approval = plan.get("byTransaction") or plan.get("approval") or plan.get("transaction")
            by_signature = plan.get("bySignature")
            summary.update({
                "ready": False,
                "requires_approval": True,
                "approval_transaction": approval,
                "permit_typed_data": by_signature,
                "permit_available": isinstance(by_signature, dict),
                "reason": plan.get("reason"),
            })

        elif typename == "PreContractActionRequired":
            first = plan.get("transaction")
            second = plan.get("originalTransaction")
            summary.update({
                "ready": False,
                "requires_ordered_steps": True,
                "transaction": first,
                "original_transaction": second,
                "first_step_present": isinstance(first, dict),
                "second_step_present": isinstance(second, dict),
            })

        elif typename == "InsufficientBalanceError":
            summary.update({
                "ready": False,
                "reason": "insufficient_balance",
            })

        else:
            summary["reason"] = "unsupported_execution_plan"

        return summary


AAVE_AGENT_WORKFLOW = (
    "discover -> inspect -> simulate -> build -> wallet-sign -> confirm"
)

AAVE_AGENT_SOURCES = {
    "agents": "https://aave.com/agents",
    "mcp": "https://mcp.aave.com",
    "mcp_docs": "https://aave.com/docs/mcp",
    "safety": "https://aave.com/docs/mcp/safety",
    "skills": "https://github.com/aave/skills",
    "skills_commit": AAVE_SKILLS_COMMIT,
}