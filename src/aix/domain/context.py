"""Context-fabric models that cross module boundaries (§15)."""

from __future__ import annotations

from pydantic import Field

from aix.domain.base import DomainModel
from aix.domain.ids import ArtifactId, DecisionId, TaskId


class Handoff(DomainModel):
    """What an accepted task passes to its dependents (§15.2).

    ``summary`` is built from facts; the agent's own words appear only inside a block labeled
    ``AGENT CLAIM (unverified)``.
    """

    task_id: TaskId
    summary: str
    files_changed: list[str] = Field(default_factory=list[str])
    decisions: list[DecisionId] = Field(default_factory=list[DecisionId])
    open_issues: list[str] = Field(default_factory=list[str])
    artifacts: list[ArtifactId] = Field(default_factory=list[ArtifactId])


class CompactedContext(DomainModel):
    """A run's handoff/decision log squeezed to fit a budget (§15.4)."""

    summary: str
    decisions: list[str] = Field(default_factory=list[str])
    open_questions: list[str] = Field(default_factory=list[str])
    known_failures: list[str] = Field(default_factory=list[str])
    important_files: list[str] = Field(default_factory=list[str])
