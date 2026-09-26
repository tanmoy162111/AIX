"""Escalation ladder (PLAYBOOK §19.3): stronger model, other agent, pair, human."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from aix.domain.agents import AgentSpec


class Step(StrEnum):
    STRONGER_MODEL = "stronger_model"
    DIFFERENT_AGENT = "different_agent"
    MULTI_AGENT = "multi_agent"
    HUMAN = "human"


@dataclass(frozen=True)
class Escalation:
    """What to do differently on the next attempt."""

    step: Step
    agent_id: str | None = None
    model: str | None = None
    require_review: bool = False
    """``multi_agent``: an independent reviewer's findings are fed back (implementer + reviewer)."""


class EscalationState:
    """How far up the ladder a task has gone."""

    def __init__(self) -> None:
        self.taken = 0

    def remaining(self, ladder: Sequence[Step]) -> bool:
        """Whether any ladder step has not been used yet."""
        return self.taken < len(ladder)


def _stronger_model(spec: AgentSpec, current: str | None) -> str | None:
    if not spec.supports.model_select or not spec.models:
        return None
    models = spec.models
    if current in models:
        idx = models.index(current) + 1
        return models[idx] if idx < len(models) else None
    return models[-1] if len(models) > 1 or current is None else None


def next_escalation(
    ladder: Sequence[Step],
    state: EscalationState,
    *,
    current: AgentSpec,
    current_model: str | None,
    candidates: Sequence[AgentSpec],
) -> Escalation | None:
    """The next applicable ladder step, advancing ``state`` past it (and any skipped steps).

    Applicability: ``stronger_model`` needs a model-selectable agent with a later model in its
    ordered ``models``; ``different_agent`` and ``multi_agent`` need another ready/degraded agent;
    ``human`` always applies. Returns ``None`` when the ladder is used up.
    """
    others = [c for c in candidates if c.id != current.id and c.health in ("ready", "degraded")]
    while state.taken < len(ladder):
        step = ladder[state.taken]
        state.taken += 1
        if step is Step.STRONGER_MODEL:
            model = _stronger_model(current, current_model)
            if model is not None:
                return Escalation(step, current.id, model)
        elif step is Step.DIFFERENT_AGENT and others:
            return Escalation(step, others[0].id)
        elif step is Step.MULTI_AGENT and others:
            return Escalation(step, current.id, require_review=True)
        elif step is Step.HUMAN:
            return Escalation(step)
    return None
