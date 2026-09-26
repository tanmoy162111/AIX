"""Deterministic TemplatePlanner (PLAYBOOK §13.2): a skill's ``workflow.yaml`` becomes a graph."""

from __future__ import annotations

import re
from typing import Final

from aix.domain.base import Risk
from aix.domain.enums import TaskType
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, RepoFacts, Task, TaskGraph, VerificationSpec
from aix.skills.registry import SkillRegistry
from aix.skills.schema import Skill, WorkflowStep

_RISK_ORDER: Final[dict[Risk, int]] = {"low": 0, "medium": 1, "high": 2}
_BUGFIX: Final = re.compile(
    r"\b(fix\w*|bugs?|crash\w*|broken|regression|fails?|failing|error|exception|defect)\b",
    re.IGNORECASE,
)
_WRITE_TESTS: Final = re.compile(
    r"^\s*(?:write|add|extend|improve|increase)\s+(?:more\s+)?(?:(?:unit|integration|e2e)\s+)?"
    r"(?:tests?|test coverage|coverage)\b",
    re.IGNORECASE,
)
_BY_KIND: Final[dict[str, str]] = {
    "review": "code-review",
    "security": "security-review",
    "docs": "documentation",
    "research": "inspect-repo",
    "devops": "feature-implementation",
    "other": "inspect-repo",
}


def select_skill(intent: Intent, registry: SkillRegistry, override: str | None = None) -> Skill:
    """Choose the skill for ``intent``.

    Contract: ``override`` wins and must exist; otherwise the choice depends only on
    ``intent.kind`` and the goal text (deterministic).

    Raises:
        ConfigError: ``override`` names an unknown skill.
    """
    if override is not None:
        return registry.get(override)
    if intent.kind == "coding":
        if _WRITE_TESTS.search(intent.goal):
            return registry.get("write-tests")
        if _BUGFIX.search(intent.goal):
            return registry.get("bugfix")
        return registry.get("feature-implementation")
    return registry.get(_BY_KIND[intent.kind])


def _included(step: WorkflowStep, skill: Skill, risk: Risk) -> bool:
    if step.when_risk_at_least is None:
        return True
    if _RISK_ORDER[risk] >= _RISK_ORDER[step.when_risk_at_least]:
        return True
    return step.type is TaskType.SECURITY_REVIEW and skill.verification.requires_security_review


class TemplatePlanner:
    """Builds the task graph from the selected skill's workflow template."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    async def plan(
        self, intent: Intent, repo_facts: RepoFacts, run_id: str, skill: str | None = None
    ) -> TaskGraph:
        """Instantiate the skill workflow for ``intent``.

        Contract: deterministic apart from generated ids; steps gated by ``when_risk_at_least``
        are dropped (unless the skill requires a security review) and dependents are rewired to
        the dropped step's own dependencies so the graph stays connected and acyclic. Write
        tasks (non-empty ``file_scope``) carry the skill's verification spec; defaults are added
        by post-processing (§13.3).

        Raises:
            ConfigError: ``skill`` names an unknown skill.
        """
        chosen = select_skill(intent, self._registry, skill)
        steps = chosen.workflow.steps
        kept = {s.id for s in steps if _included(s, chosen, intent.risk)}
        deps_of = {s.id: s.depends_on for s in steps}

        def resolve(step_id: str) -> list[str]:
            if step_id in kept:
                return [step_id]
            return [d for parent in deps_of[step_id] for d in resolve(parent)]

        ids = {s.id: new_id(IdPrefix.TASK) for s in steps if s.id in kept}
        spec = VerificationSpec(
            required=list(chosen.verification.required),
            optional=list(chosen.verification.optional),
        )
        tasks: list[Task] = []
        for step in steps:
            if step.id not in kept:
                continue
            deps = list(
                dict.fromkeys(ids[d] for parent in step.depends_on for d in resolve(parent))
            )
            tasks.append(
                Task(
                    id=ids[step.id],
                    run_id=run_id,
                    title=step.title,
                    goal=step.goal.replace("{goal}", intent.goal),
                    type=step.type,
                    skill=chosen.meta.name,
                    required_capabilities=list(step.required_capabilities),
                    depends_on=deps,
                    file_scope=list(step.file_scope),
                    verification=spec if step.file_scope else VerificationSpec(),
                    risk=intent.risk,
                )
            )
        return TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=tasks)
