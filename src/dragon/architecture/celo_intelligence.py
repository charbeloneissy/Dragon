from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, Sequence

from .contracts import BrainSource, IntelligenceFact


@dataclass(frozen=True)
class CeloIntelligenceQuery:
    topic: str
    chain_id: int = 42220
    block_number: int | None = None


class CeloIntelligenceProvider(Protocol):
    def query(self, request: CeloIntelligenceQuery) -> Sequence[IntelligenceFact]:
        """Return factual, source-tagged intelligence; never execution commands."""


class CeloFactFusion:
    """Merge Celo AI/Celopedia facts with live on-chain facts.

    On-chain observations win conflicts for executable state.  AI knowledge can
    enrich discovery and interpretation but cannot authorize execution.
    """

    def __init__(self, *, max_age_ms: int = 120_000) -> None:
        self.max_age_ms = max_age_ms

    def normalize(
        self,
        *,
        now_ms: int,
        ai_facts: Sequence[IntelligenceFact],
        chain_facts: Sequence[IntelligenceFact],
    ) -> tuple[IntelligenceFact, ...]:
        merged: dict[str, IntelligenceFact] = {}

        for fact in ai_facts:
            if fact.expires_at_ms is not None and fact.expires_at_ms < now_ms:
                continue
            if now_ms - fact.observed_at_ms > self.max_age_ms:
                continue
            merged[fact.key] = fact

        for fact in chain_facts:
            if now_ms - fact.observed_at_ms > self.max_age_ms:
                continue
            # Live chain facts are authoritative for the same key.
            merged[fact.key] = fact

        return tuple(merged.values())

    @staticmethod
    def execution_authority(facts: Sequence[IntelligenceFact]) -> bool:
        """Always false: intelligence is never an execution authority."""
        return False
