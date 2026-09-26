"""Decision provider interface (PLAYBOOK §18.4)."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import Field, JsonValue

from aix.decision.state import DecisionState
from aix.domain.base import DomainModel
from aix.domain.enums import DecisionOutcome, DecisionPoint

ProviderName = Literal["rules", "jev", "fake", "human"]


class ProviderAnswer(DomainModel):
    """What a provider chose, with the questions and answers behind it (for the record)."""

    outcome: DecisionOutcome
    choice: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    questions: dict[str, JsonValue] = Field(default_factory=dict)
    answers: dict[str, JsonValue] = Field(default_factory=dict)
    meta: dict[str, JsonValue] = Field(default_factory=dict)


class DecisionProvider(Protocol):
    """Chooses among ``allowed`` outcomes for a decision point from control-plane facts."""

    name: ProviderName

    async def decide(
        self, point: DecisionPoint, state: DecisionState, allowed: set[DecisionOutcome]
    ) -> ProviderAnswer:
        """Return an answer whose outcome is in ``allowed``. May raise; the service falls back."""
        ...
