"""Vendor-neutral Jev client interface and a scripted fake (PLAYBOOK §18.4).

The real client (``providers/jev.py``, the only module allowed to import the SDK) and the fake
both implement :class:`JevClient`; everything else in aiX depends on this module only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, Self, runtime_checkable

from pydantic import Field, JsonValue, model_validator

from aix.domain.base import DomainModel


class JevError(Exception):
    """Jev could not answer (network, auth, timeout, malformed response)."""


class JevQuestion(DomainModel):
    """One typed question: a choice among named criteria, a score over ordered levels, or a noul."""

    kind: Literal["choice", "score", "noul"]
    instructions: str
    criteria: dict[str, str] = Field(default_factory=dict)
    """``choice``: option name -> description."""
    levels: list[str] = Field(default_factory=list)
    """``score``: ordered level descriptions (index = level)."""
    true_means: str | None = None
    """``noul``: optional description of the yes outcome."""
    false_means: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind == "choice" and len(self.criteria) < 2:
            raise ValueError("a choice question needs at least two criteria")
        if self.kind == "score" and len(self.levels) < 2:
            raise ValueError("a score question needs at least two levels")
        return self


def choice_q(instructions: str, criteria: Mapping[str, str]) -> JevQuestion:
    """A ``Choice`` question."""
    return JevQuestion(kind="choice", instructions=instructions, criteria=dict(criteria))


def score_q(instructions: str, levels: Sequence[str]) -> JevQuestion:
    """A ``Score`` question over ordered ``levels``."""
    return JevQuestion(kind="score", instructions=instructions, levels=list(levels))


def noul_q(
    instructions: str, *, true_means: str | None = None, false_means: str | None = None
) -> JevQuestion:
    """A ``Noul`` (yes/no probability) question."""
    return JevQuestion(
        kind="noul", instructions=instructions, true_means=true_means, false_means=false_means
    )


class JevAnswer(DomainModel):
    """An answer; the field that matches ``kind`` is set."""

    kind: Literal["choice", "score", "noul"]
    choice: str | None = None
    score: float | None = None
    noul: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None


class JevResponse(DomainModel):
    answers: dict[str, JevAnswer]
    model: str | None = None
    usage: dict[str, int | None] = Field(default_factory=dict)


@runtime_checkable
class JevClient(Protocol):
    """Asks Jev a set of typed questions about a compact state."""

    async def system_one(
        self, state: dict[str, JsonValue] | str, questions: Mapping[str, JevQuestion]
    ) -> JevResponse:
        """Answer every question. Raises :class:`JevError` on any failure."""
        ...


@dataclass(frozen=True)
class FakeCall:
    state: dict[str, JsonValue] | str
    questions: dict[str, JevQuestion]


@dataclass
class FakeJevClient:
    """Scripted Jev for tests: answers keyed by question name, or a per-call sequence."""

    answers: Mapping[str, JevAnswer] = field(default_factory=dict[str, JevAnswer])
    sequence: Sequence[Mapping[str, JevAnswer]] | None = None
    error: Exception | None = None
    calls: list[FakeCall] = field(default_factory=list[FakeCall])

    async def system_one(
        self, state: dict[str, JsonValue] | str, questions: Mapping[str, JevQuestion]
    ) -> JevResponse:
        """Record the call, then raise ``error`` or return the scripted answers asked for."""
        self.calls.append(FakeCall(state, dict(questions)))
        if self.error is not None:
            raise self.error
        script = self.answers
        if self.sequence is not None:
            script = self.sequence[min(len(self.calls) - 1, len(self.sequence) - 1)]
        return JevResponse(
            answers={name: script[name] for name in questions if name in script}, model="fake"
        )
