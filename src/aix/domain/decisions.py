"""GateResult, DecisionRecord and Approval (§6, §18, §20.4)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, JsonValue, model_validator

from aix.domain.base import DomainModel, Sha256, UtcDatetime
from aix.domain.enums import DecisionOutcome, DecisionPoint
from aix.domain.ids import ApprovalId, DecisionId


class GateResult(DomainModel):
    """Result of the hard gates (§18.3): either a forced outcome or a set of allowed outcomes."""

    forced_outcome: DecisionOutcome | None = None
    allowed_outcomes: list[DecisionOutcome] = Field(default_factory=list[DecisionOutcome])
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _forced_is_allowed(self) -> Self:
        if (
            self.forced_outcome is not None
            and self.allowed_outcomes
            and self.forced_outcome not in self.allowed_outcomes
        ):
            raise ValueError("forced_outcome must be one of allowed_outcomes")
        return self


class DecisionRecord(DomainModel):
    """A recorded, replayable decision (§18.1 rule 4)."""

    id: DecisionId
    point: DecisionPoint
    subject: str
    """Task, attempt or tool-call id."""
    provider: Literal["rules", "jev", "fake", "human"]
    gate_result: GateResult
    state: dict[str, JsonValue]
    """Exact, sanitized state sent to the provider."""
    questions: dict[str, JsonValue]
    answers: dict[str, JsonValue]
    outcome: DecisionOutcome
    choice: str | None = None
    """The picked candidate when ``outcome`` is ``choose`` (spec's ``choose:<x>``); see ADR-0005."""
    reason_codes: list[str] = Field(default_factory=list)
    provider_meta: dict[str, JsonValue] = Field(default_factory=dict)
    inputs_hash: Sha256
    """SHA-256 of canonical(state, questions, policy version)."""

    @model_validator(mode="after")
    def _choice_iff_choose(self) -> Self:
        if (self.outcome is DecisionOutcome.CHOOSE) != (self.choice is not None):
            raise ValueError("choice must be set exactly when outcome is 'choose'")
        return self


class Approval(DomainModel):
    """A human grant (or refusal) for a gated action."""

    id: ApprovalId
    subject: str
    action: str
    scope: dict[str, JsonValue] = Field(default_factory=dict)
    requested_at: UtcDatetime
    status: Literal["pending", "granted", "denied", "expired"] = "pending"
    actor: str | None = None
    channel: Literal["cli_tty", "api_token"] | None = None
    decided_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _decided_states_are_attributed(self) -> Self:
        if self.status in ("granted", "denied") and (
            not self.actor or self.channel is None or self.decided_at is None
        ):
            raise ValueError(f"a {self.status} approval needs actor, channel and decided_at")
        return self
