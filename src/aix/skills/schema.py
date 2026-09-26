"""Schemas for the four files of a skill (PLAYBOOK §16)."""

from __future__ import annotations

from typing import Self

from pydantic import Field, model_validator

from aix.domain.base import DomainModel, Risk
from aix.domain.enums import Capability, CheckKind, TaskType

SKILL_NAME = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"


class SkillMeta(DomainModel):
    """``skill.yaml``: identity and what the skill applies to."""

    name: str = Field(pattern=SKILL_NAME)
    description: str = Field(min_length=1)
    task_types: list[TaskType] = Field(min_length=1)
    required_capabilities: list[Capability] = Field(default_factory=list[Capability])
    inputs: list[str] = Field(default_factory=list)


class WorkflowStep(DomainModel):
    """One task in the template graph. ``goal`` may use ``{goal}`` for the run goal."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: TaskType
    title: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    file_scope: list[str] = Field(default_factory=list)
    """Globs the step may write; empty means read-only (§6)."""
    required_capabilities: list[Capability] = Field(default_factory=list[Capability])
    when_risk_at_least: Risk | None = None
    """Include the step only when the intent risk is at least this level (§13.2)."""


class Workflow(DomainModel):
    """``workflow.yaml``: the task template consumed by the TemplatePlanner."""

    steps: list[WorkflowStep] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_graph(self) -> Self:
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate step ids")
        remaining = {s.id: set(s.depends_on) for s in self.steps}
        for step_id, deps in remaining.items():
            unknown = deps - set(ids)
            if unknown:
                raise ValueError(f"step {step_id!r} depends on unknown step {sorted(unknown)[0]!r}")
        while remaining:
            ready = [i for i, deps in remaining.items() if not deps]
            if not ready:
                raise ValueError("workflow contains a cycle")
            for i in ready:
                del remaining[i]
            for deps in remaining.values():
                deps.difference_update(ready)
        return self


class CustomCheck(DomainModel):
    """A skill-supplied check (``custom`` kind, §17.3)."""

    name: str = Field(min_length=1)
    command: list[str] = Field(min_length=1)
    pass_criteria: str = "exit_code_0"


class SkillVerification(DomainModel):
    """``verification.yaml``: required/optional checks the skill adds to its write tasks."""

    required: list[CheckKind] = Field(default_factory=list[CheckKind])
    optional: list[CheckKind] = Field(default_factory=list[CheckKind])
    custom: list[CustomCheck] = Field(default_factory=list[CustomCheck])
    requires_security_review: bool = False


class Skill(DomainModel):
    """A fully loaded and validated skill."""

    meta: SkillMeta
    instructions: str
    workflow: Workflow
    verification: SkillVerification

    @model_validator(mode="after")
    def _steps_use_declared_task_types(self) -> Self:
        allowed = set(self.meta.task_types)
        for step in self.workflow.steps:
            if step.type not in allowed:
                raise ValueError(f"step {step.id!r} has type {step.type.value!r} not in task_types")
        return self
