from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


ERROR_LEVEL = "error"
WARNING_LEVEL = "warning"
INFO_LEVEL = "info"


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
    """Dragon-side implementation of Aave's discover -> inspect -> simulate -> build -> confirm flow.

    This policy never signs, broadcasts, or creates custody. It governs which
    prepared actions Dragon may present to a signer.
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
            item for item in raw
            if isinstance(item, dict) and str(item.get("level", "")).lower() in {
                ERROR_LEVEL, WARNING_LEVEL, INFO_LEVEL
            }
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
        for warning in warnings:
            if str(warning.get("level", "")).lower() == ERROR_LEVEL:
                return AaveAgentDecision(
                    allowed=False,
                    phase=phase,
                    reason=f"error_warning:{warning.get('code', 'unknown')}",
                    warnings=warnings,
                )

        if list(chains_not_served):
            return AaveAgentDecision(
                allowed=False,
                phase=phase,
                reason="requested_chain_not_served",
                warnings=warnings,
            )

        # An explicitly incomplete read cannot be treated as a zero-result read.
        if list(chains_not_covered):
            return AaveAgentDecision(
                allowed=False,
                phase=phase,
                reason="required_chain_not_covered",
                warnings=warnings,
            )

        if phase in {"borrow", "withdraw", "repay", "supply"} and self.require_simulation:
            if not isinstance(result, dict) or not result.get("_dragon_simulation_ok", False):
                return AaveAgentDecision(
                    allowed=False,
                    phase=phase,
                    reason="simulation_required",
                    warnings=warnings,
                )

        if phase == "build":
            # Building is allowed when the policy has passed previous phases;
            # signing remains outside this module.
            return AaveAgentDecision(
                allowed=True,
                phase=phase,
                reason="unsigned_transaction_ready",
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
        if not isinstance(plan, dict):
            return {"type": type(plan).__name__, "ready": False}

        typename = str(plan.get("__typename") or plan.get("operation") or "unknown")
        summary = {"type": typename, "ready": False}

        if typename == "TransactionRequest":
            summary.update({
                "ready": True,
                "chainId": plan.get("chainId"),
                "to": plan.get("to"),
                "from": plan.get("from"),
                "operation": plan.get("operation"),
                "requires_wallet_signature": True,
            })
        elif typename in {"ApprovalRequired", "Erc20ApprovalRequired"}:
            summary.update({
                "ready": False,
                "requires_approval": True,
                "reason": plan.get("reason"),
            })
        elif typename == "PreContractActionRequired":
            summary.update({
                "ready": False,
                "requires_ordered_steps": True,
            })
        elif typename == "InsufficientBalanceError":
            summary.update({
                "ready": False,
                "reason": "insufficient_balance",
            })
        else:
            summary["ready"] = False

        return summary


AAVE_AGENT_WORKFLOW = (
    "discover -> inspect -> simulate -> build -> wallet-sign -> confirm"
)

AAVE_AGENT_SOURCES = {
    "agents": "https://aave.com/agents",
    "mcp": "https://mcp.aave.com",
    "safety": "https://aave.com/docs/mcp/safety",
    "skills": "https://github.com/aave/skills",
}
