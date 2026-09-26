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
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.planner.agent import AgentPlanner, make_adapter_runner
from aix.core.planner.postprocess import postprocess_plan
from aix.core.planner.select import choose_planner
from aix.core.planner.template import TemplatePlanner
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.errors import classify
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import RunEvent
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


@dataclass(frozen=True)
class PlannerSetup:
    """Planner decision made before the run exists (an agent planner needs the run branch)."""

    agent_id: str | None
    warnings: list[str]


async def choose(
    req: PlanRunRequest, registry: AdapterRegistry, config: AixConfig, skills: SkillRegistry
) -> PlannerSetup:
    """Validate the skill override and resolve the planner provider (may raise ``ConfigError``)."""
    if req.skill is not None:
        skills.get(req.skill)  # fail fast on a typo, whichever planner runs
    provider = (
        config.planner.model_copy(update={"provider": f"agent:{req.planner_agent}"})
        if req.planner_agent
        else config.planner
    )
    choice = await choose_planner(provider, registry)
    return PlannerSetup(choice.agent_id, list(choice.warnings))


async def record_plan(
    rec: RunRecorder, intent: Intent, graph: TaskGraph, planner: str, warnings: list[str]
) -> None:
    """Record ``run.planned`` and a ``task.created`` per task; the run moves to ``planned``."""
    await rec.emit(
        "run.planned",
        ev.RunPlannedPayload(intent=intent, graph=graph, planner=planner, warnings=warnings),
    )
    for task in graph.tasks:
        await rec.emit("task.created", ev.TaskCreatedPayload(task=task), task_id=task.id)
    await rec.run_to(RunEvent.PLANNED)


async def plan_into(
    rec: RunRecorder,
    req: PlanRunRequest,
    setup: PlannerSetup,
    *,
    wm: WorkspaceManager,
    registry: AdapterRegistry,
    config: AixConfig,
    skills: SkillRegistry,
) -> PlanRunResult:
    """Plan inside an already-started run (status ``planning``) and end in ``planned``.

    Contract: emits ``run.planned`` with the post-processed graph, one ``task.created`` per task,
    then ``run.state_changed`` (planned). If planning itself raises, the run is failed
    (``run.failed``) and the error propagates. An agent planner needs the run branch to exist.
    """
    warnings = list(setup.warnings)
    try:
        facts = await anyio.to_thread.run_sync(inspect_repo, wm.root)
        intent = RulesIntentEngine().parse(req.goal, facts)
        template = TemplatePlanner(skills)
        planner_name = "template"
        run_id = rec.run.id
        if setup.agent_id is None:
            graph = await template.plan(intent, facts, run_id, req.skill)
        else:
            agent_id = setup.agent_id
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
        await rec.run_to(RunEvent.FAIL)
        await rec.emit("run.failed", ev.RunFailedPayload(failure=classify(exc), reason=str(exc)))
        raise

    await record_plan(rec, intent, graph, planner_name, warnings)
    return PlanRunResult(
        run_id=run_id, intent=intent, graph=graph, planner=planner_name, warnings=warnings
    )


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

    The template planner needs no git repository; an agent planner runs read-only in a throwaway
    worktree of the run branch, which is created only in that case.

    Raises:
        ConfigError: ``planner.provider`` names an unknown agent, or ``req.skill`` is unknown.
        ToolFailure: an agent planner is used but the project is not a clean git repository.
    """
    skills = skills or SkillRegistry.builtin()
    wm = WorkspaceManager(req.project_root)
    setup = await choose(req, registry, config, skills)
    run_id = new_id(IdPrefix.RUN)
    if setup.agent_id is not None:
        await wm.create_run_branch(run_id, allow_dirty=req.allow_dirty)
    rec = RunRecorder(store, new_run(run_id, wm.root, req.goal, config, clock()), clock)
    await rec.start()
    return await plan_into(rec, req, setup, wm=wm, registry=registry, config=config, skills=skills)
