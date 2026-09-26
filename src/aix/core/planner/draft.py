"""The plan an agent proposes (PLAYBOOK §13.2, Appendix B.1).

Agents cannot mint ULIDs, so the wire format uses short local ``key`` strings. Conversion to a
real :class:`~aix.domain.tasks.TaskGraph` (which enforces the graph invariants) happens in
``aix.core.planner.agent.parse_plan``.
"""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from aix.domain.base import DomainModel, Risk
from aix.domain.enums import CheckKind, TaskType


class DraftVerification(DomainModel):
    """Checks proposed for a task."""

    required: list[CheckKind] = Field(default_factory=list[CheckKind])
    optional: list[CheckKind] = Field(default_factory=list[CheckKind])


class DraftTask(DomainModel):
    """One proposed task; ``depends_on`` lists other tasks' ``key`` values."""

    key: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,40}$")
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    type: TaskType
    skill: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    """Free strings on the wire; unknown names are dropped with a warning (§13.3)."""
    depends_on: list[str] = Field(default_factory=list)
    file_scope: list[str] = Field(default_factory=list)
    verification: DraftVerification = Field(default_factory=DraftVerification)
    risk: Risk | None = None


class PlanDraft(DomainModel):
    """The JSON object an agent planner must return."""

    tasks: list[DraftTask]

    @model_validator(mode="after")
    def _valid_graph(self) -> Self:
        if not self.tasks:
            raise ValueError("a plan needs at least one task")
        keys = [t.key for t in self.tasks]
        if len(set(keys)) != len(keys):
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            raise ValueError(f"duplicate task keys: {', '.join(dupes)}")
        remaining = {t.key: set(t.depends_on) for t in self.tasks}
        for key, deps in remaining.items():
            if key in deps:
                raise ValueError(f"task {key!r} depends on itself")
            unknown = sorted(deps - set(keys))
            if unknown:
                raise ValueError(f"task {key!r} has unknown dependency {unknown[0]!r}")
        while remaining:
            ready = [k for k, deps in remaining.items() if not deps]
            if not ready:
                raise ValueError("task graph contains a cycle")
            for k in ready:
                del remaining[k]
            for deps in remaining.values():
                deps.difference_update(ready)
        return self
