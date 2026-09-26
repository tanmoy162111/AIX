"""Deterministic ``rules`` routing strategy (PLAYBOOK §12)."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from aix.config.schema import RoutingConfig
from aix.domain.agents import AgentSpec
from aix.domain.base import DomainModel
from aix.domain.enums import TaskType
from aix.domain.errors import NoEligibleAgent
from aix.domain.tasks import Task

W_OBSERVED: Final = 0.45
W_PRIOR: Final = 0.35
W_COST: Final = 0.10
W_LATENCY: Final = 0.10
PENALTY_FAILED_BEFORE: Final = 0.5
PENALTY_NOT_INDEPENDENT: Final = 1.0
FALLBACKS: Final = 2
_COST_FIT: Final = {"free": 1.0, "low": 0.75, "medium": 0.5, "high": 0.25}
_LATENCY_HALF_SCORE_S: Final = 300.0
_REVIEW_TYPES: Final = frozenset({TaskType.REVIEW, TaskType.SECURITY_REVIEW})


@dataclass(frozen=True)
class AgentStat:
    """Observed history of one agent on one task type (§22.3); all fields optional."""

    attempts: int = 0
    accepted: int = 0
    mean_cost_usd: float | None = None
    p50_latency_s: float | None = None


def _allow_all(_agent: AgentSpec, _task: Task) -> bool:
    return True


@dataclass(frozen=True)
class RoutingContext:
    """Everything the router needs; callers assemble it from probes, projections and policy."""

    task: Task
    agents: Sequence[AgentSpec]
    """Enabled agents with their current probe result."""
    stats: Mapping[tuple[str, TaskType], AgentStat] = field(
        default_factory=dict[tuple[str, TaskType], AgentStat]
    )
    failed_agents: frozenset[str] = frozenset()
    """Agents that already failed this task."""
    same_agent_new_context: bool = False
    """The retry mutation that intentionally reuses a failed agent (§19.3)."""
    authored_by: str | None = None
    """Agent that authored the change under review, for review tasks."""
    budget_remaining_usd: float | None = None
    expected_cost_usd: Mapping[str, float] = field(default_factory=dict[str, float])
    config: RoutingConfig = field(default_factory=RoutingConfig)
    policy_allows: Callable[[AgentSpec, Task], bool] = _allow_all


class RoutingDecision(DomainModel):
    """Result of routing; emitted as ``agent.selected``."""

    primary: str
    fallbacks: list[str]
    scores: dict[str, float]
    reason_codes: list[str]


def bayesian_success_rate(accepted: int, attempts: int) -> float:
    """Posterior mean success rate under a Beta(2, 2) prior: ``(accepted + 2) / (attempts + 4)``."""
    return (accepted + 2) / (attempts + 4)


def _supports(agent: AgentSpec, task: Task) -> str | None:
    """``None`` if the agent supports every required capability, else the first missing one."""
    for cap in task.required_capabilities:
        if agent.capabilities.get(cap, 0.0) <= 0.0:
            return cap.value
    return None


def _score(agent: AgentSpec, ctx: RoutingContext) -> float:
    task = ctx.task
    caps = [agent.capabilities[c] for c in task.required_capabilities]
    prior = sum(caps) / len(caps) if caps else 0.5
    stat = ctx.stats.get((agent.id, task.type), AgentStat())
    observed = bayesian_success_rate(stat.accepted, stat.attempts)
    cost_fit = _COST_FIT[agent.cost_class]
    latency_fit = (
        0.5
        if stat.p50_latency_s is None
        else 1.0 / (1.0 + max(stat.p50_latency_s, 0.0) / _LATENCY_HALF_SCORE_S)
    )
    return W_OBSERVED * observed + W_PRIOR * prior + W_COST * cost_fit + W_LATENCY * latency_fit


def route(ctx: RoutingContext) -> RoutingDecision:
    """Pick the primary agent and up to two fallbacks for ``ctx.task``.

    Contract (§12): eligible = health ready/degraded (degraded only when no ready agent remains),
    every required capability with prior > 0, policy allows, and expected cost within the
    remaining budget. ``score = 0.45*observed + 0.35*prior + 0.10*cost_fit + 0.10*latency_fit``
    minus penalties (-0.5 already failed this task unless ``same_agent_new_context``; -1.0 for a
    reviewer who authored the change when ``prefer_independent_reviewer``). A static pin for the
    task type wins over scoring if the pinned agent is eligible. Pure and deterministic: ties
    break by agent id.

    Raises:
        NoEligibleAgent: no agent survives eligibility; the message lists why each was dropped.
    """
    task, cfg = ctx.task, ctx.config
    reasons = [f"strategy:{cfg.strategy}"]
    dropped: list[str] = []

    eligible: list[AgentSpec] = []
    for agent in sorted(ctx.agents, key=lambda a: a.id):
        if agent.health not in ("ready", "degraded"):
            dropped.append(f"{agent.id}: {agent.health}")
        elif (missing := _supports(agent, task)) is not None:
            dropped.append(f"{agent.id}: missing capability {missing}")
        elif not ctx.policy_allows(agent, task):
            dropped.append(f"{agent.id}: forbidden by policy")
        else:
            eligible.append(agent)

    if any(a.health == "ready" for a in eligible):
        for a in eligible:
            if a.health == "degraded":
                dropped.append(f"{a.id}: degraded while ready agents exist")
        eligible = [a for a in eligible if a.health == "ready"]
    elif eligible:
        reasons.append("degraded_only")

    affordable: list[AgentSpec] = []
    for agent in eligible:
        expected = ctx.expected_cost_usd.get(agent.id)
        if (
            ctx.budget_remaining_usd is not None
            and expected is not None
            and expected > ctx.budget_remaining_usd
        ):
            reasons.append(f"over_budget:{agent.id}")
            dropped.append(f"{agent.id}: expected cost ${expected:.2f} exceeds remaining budget")
        else:
            affordable.append(agent)

    if not affordable:
        raise NoEligibleAgent(
            f"no eligible agent for task {task.title!r} ({task.type.value}): "
            f"{'; '.join(dropped) or 'no agents are registered'}",
            details={"task_id": task.id, "reasons": [*reasons, *dropped]},
        )
    reasons.append(f"eligible:{len(affordable)}")

    scores: dict[str, float] = {}
    for agent in affordable:
        score = _score(agent, ctx)
        if agent.id in ctx.failed_agents and not ctx.same_agent_new_context:
            score -= PENALTY_FAILED_BEFORE
            reasons.append(f"penalty:failed_before:{agent.id}")
        if (
            task.type in _REVIEW_TYPES
            and cfg.prefer_independent_reviewer
            and ctx.authored_by == agent.id
        ):
            score -= PENALTY_NOT_INDEPENDENT
            reasons.append(f"penalty:not_independent:{agent.id}")
        scores[agent.id] = score
    assert all(math.isfinite(s) for s in scores.values())

    ranked = sorted(scores, key=lambda i: (-scores[i], i))
    primary = ranked[0]
    pin = cfg.static.get(task.type.value)
    if pin is not None:
        if pin in scores:
            primary = pin
            reasons.append(f"static_pin:{pin}")
        else:
            reasons.append(f"static_pin_ineligible:{pin}")
    fallbacks = [i for i in ranked if i != primary][:FALLBACKS]
    return RoutingDecision(
        primary=primary,
        fallbacks=fallbacks,
        scores={i: round(scores[i], 4) for i in ranked},
        reason_codes=reasons,
    )
