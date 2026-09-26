"""Intent, Task and TaskGraph (§6)."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from aix.domain.base import DomainModel, Risk
from aix.domain.enums import Capability, CheckKind, TaskStatus, TaskType
from aix.domain.ids import GraphId, RunId, TaskId


class Intent(DomainModel):
    """A parsed user goal."""

    goal: str
    kind: Literal["coding", "research", "review", "security", "devops", "docs", "other"]
    risk: Risk
    constraints: list[str] = Field(default_factory=list)
    target_paths: list[str] = Field(default_factory=list)


class VerificationSpec(DomainModel):
    """Which checks must / may run for a task."""

    required: list[CheckKind] = Field(default_factory=list[CheckKind])
    optional: list[CheckKind] = Field(default_factory=list[CheckKind])


class Task(DomainModel):
    """One unit of work in a graph."""

    id: TaskId
    run_id: RunId
    title: str = Field(min_length=1)
    goal: str
    type: TaskType
    skill: str | None = None
    required_capabilities: list[Capability] = Field(default_factory=list[Capability])
    depends_on: list[TaskId] = Field(default_factory=list)
    file_scope: list[str] = Field(default_factory=list)
    """Globs the task is expected to touch; an empty list means read-only."""
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    risk: Risk = "low"
    max_attempts: int = Field(default=3, ge=1, le=6)
    status: TaskStatus = TaskStatus.CREATED

    @model_validator(mode="after")
    def _no_self_dependency(self) -> Self:
        if self.id in self.depends_on:
            raise ValueError(f"task {self.id} depends on itself")
        return self


class TaskGraph(DomainModel):
    """A run's task DAG.

    Invariants: at least one task, unique ids, all dependencies exist, acyclic, and every task
    belongs to ``run_id``. Implicit file-scope edges are added by the planner (§14.3), not here.
    """

    id: GraphId
    run_id: RunId
    tasks: list[Task]

    @model_validator(mode="after")
    def _check_invariants(self) -> Self:
        if not self.tasks:
            raise ValueError("a task graph needs at least one task")
        ids = [t.id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("task ids must be unique")
        known = set(ids)
        for t in self.tasks:
            if t.run_id != self.run_id:
                raise ValueError(f"task {t.id} has run_id {t.run_id}, expected {self.run_id}")
            for dep in t.depends_on:
                if dep not in known:
                    raise ValueError(f"task {t.id} has unknown dependency {dep}")
        if len(self._toposort()) != len(self.tasks):
            raise ValueError("task graph contains a cycle")
        return self

    def _toposort(self) -> list[Task]:
        """Kahn's algorithm; stable with respect to the declared task order."""
        remaining = {t.id: set(t.depends_on) for t in self.tasks}
        by_id = {t.id: t for t in self.tasks}
        order: list[Task] = []
        while remaining:
            ready = [tid for tid, deps in remaining.items() if not deps]
            if not ready:
                break
            for tid in ready:
                order.append(by_id[tid])
                del remaining[tid]
            for deps in remaining.values():
                deps.difference_update(ready)
        return order

    def topological_order(self) -> list[Task]:
        """Return tasks so that every task appears after all of its dependencies."""
        return self._toposort()


class RepoFacts(DomainModel):
    """Cheap, local facts about a repository (§13.1, §15.1). No agent prose, no file contents."""

    languages: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    test_commands: list[str] = Field(default_factory=list)
    convention_files: list[str] = Field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    is_git_repo: bool = False
