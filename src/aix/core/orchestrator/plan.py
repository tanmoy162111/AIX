"""Planning phase of a run: goal -> intent -> task graph, recorded as events (PLAYBOOK §13)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio

from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
from aix.core.intent.engine import RulesIntentEngine
from aix.core.planner.agent import AgentPlanner, make_adapter_runner
from aix.core.planner.postprocess import postprocess_plan
from aix.core.planner.select import choose_planner
from aix.core.planner.template import TemplatePlanner
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.errors import classify
from aix.domain.ids import IdPrefix, new_id
from aix.domain.runs import Budget, Run
from aix.domain.state import RunEvent, transition_run
from aix.domain.tasks import Intent, TaskGraph
from aix.skills.registry import SkillRegistry
from aix.store import events as ev
from aix.store.db import EventStore
from aix.verification.detect import inspect_repo


@dataclass(frozen=True)
class PlanRunRequest:
    project_root: Path
    goal: str
    skill: str | None = None
    planner_agent: str | None = None
    """Overrides ``planner.provider`` with ``agent:<id>`` (``aix run --plan-only --agent``)."""
    allow_dirty: bool = False


@dataclass(frozen=True)
class PlanRunResult:
    run_id: str
    intent: Intent
    graph: TaskGraph
    planner: str
    warnings: list[str] = field(default_factory=list[str])


def _utc() -> datetime:
    return datetime.now(UTC)


async def plan_run(
    req: PlanRunRequest,
    *,
    registry: AdapterRegistry,
    store: EventStore,
    config: AixConfig,
    skills: SkillRegistry | None = None,
    clock: Callable[[], datetime] = _utc,
) -> PlanRunResult:
    """Create a run, plan it and stop in status ``planned`` (``aix run --plan-only``).

    Contract: emits ``run.created``, ``run.state_changed`` (planning), ``run.planned`` with the
    post-processed graph, one ``task.created`` per task, then ``run.state_changed`` (planned). The
    template planner needs no git repository; an agent planner runs read-only in a throwaway
    worktree of the run branch, which is created only in that case. If planning itself raises,
    the run is failed (``run.failed``) and the error propagates.

    Raises:
        ConfigError: ``planner.provider`` names an unknown agent, or ``req.skill`` is unknown.
        ToolFailure: an agent planner is used but the project is not a clean git repository.
    """
    skills = skills or SkillRegistry.builtin()
    if req.skill is not None:
        skills.get(req.skill)  # fail fast on a typo, whichever planner runs
    wm = WorkspaceManager(req.project_root)
    provider = (
        config.planner.model_copy(update={"provider": f"agent:{req.planner_agent}"})
        if req.planner_agent
        else config.planner
    )
    choice = await choose_planner(provider, registry)
    warnings = list(choice.warnings)

    run_id = new_id(IdPrefix.RUN)
    if choice.agent_id is not None:
        await wm.create_run_branch(run_id, allow_dirty=req.allow_dirty)

    async def emit(etype: str, payload: ev.Payload, *, task_id: str | None = None) -> None:
        await store.append(etype, payload, run_id=run_id, task_id=task_id, ts=clock())

    run = Run(
        id=run_id,
        project_root=wm.root,
        goal=req.goal,
        budget=Budget(
            max_cost_usd=config.budget.max_cost_usd_per_run,
            max_attempts_total=config.budget.max_attempts_per_run,
            max_wall_seconds=config.budget.max_wall_seconds_per_run,
        ),
        created_at=clock(),
    )

    async def run_to(event: RunEvent) -> None:
        nonlocal run
        new = transition_run(run, event, at=clock())
        await emit(
            "run.state_changed",
            ev.RunStateChangedPayload(from_status=run.status, to_status=new.status, event=event),
        )
        run = new

    await emit("run.created", ev.RunCreatedPayload(run=run))
    await run_to(RunEvent.START_PLANNING)
    try:
        facts = await anyio.to_thread.run_sync(inspect_repo, wm.root)
        intent = RulesIntentEngine().parse(req.goal, facts)
        template = TemplatePlanner(skills)
        planner_name = "template"
        if choice.agent_id is None:
            graph = await template.plan(intent, facts, run_id, req.skill)
        else:
            agent_id = choice.agent_id
            override = config.agents.overrides.get(agent_id)
            runner = make_adapter_runner(
                registry.get(agent_id),
                wm,
                run_id,
                timeout_s=(override.timeout_s if override and override.timeout_s else None)
                or config.execution.attempt_timeout_s,
                model=override.model if override else None,
            )
            outcome = await AgentPlanner(
                skills, runner, template, max_tasks=config.planner.max_tasks
            ).plan_with_report(intent, facts, run_id, req.skill)
            graph = outcome.graph
            warnings += outcome.warnings
            if outcome.source == "agent":
                planner_name = f"agent:{agent_id}"
            else:
                detail = outcome.errors[-1] if outcome.errors else "no reason recorded"
                warnings.append(
                    f"planner agent {agent_id!r} produced no valid plan; used template fallback: "
                    f"{detail}"
                )
        graph, post_warnings = postprocess_plan(
            graph,
            max_tasks=config.planner.max_tasks,
            required_default=config.verification.required_default,
            registry=skills,
        )
        warnings += post_warnings
    except Exception as exc:
        await run_to(RunEvent.FAIL)
        await emit("run.failed", ev.RunFailedPayload(failure=classify(exc), reason=str(exc)))
        raise

    await emit(
        "run.planned",
        ev.RunPlannedPayload(intent=intent, graph=graph, planner=planner_name, warnings=warnings),
    )
    for task in graph.tasks:
        await emit("task.created", ev.TaskCreatedPayload(task=task), task_id=task.id)
    await run_to(RunEvent.PLANNED)
    return PlanRunResult(
        run_id=run_id, intent=intent, graph=graph, planner=planner_name, warnings=warnings
    )
