"""`aix run` (PLAYBOOK §23.1): routed multi-task runs, `--plan-only`, or `--agent` single task."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path
from typing import Annotated

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit, registry_or_exit


def run(
    goal: Annotated[str, typer.Argument(help="What you want done.")],
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent", help="Agent id (routing arrives in M3.7); the planner for --plan-only."
        ),
    ] = None,
    plan_only: Annotated[
        bool, typer.Option("--plan-only", help="Plan the run, print the plan and stop.")
    ] = False,
    skill: Annotated[
        str | None, typer.Option("--skill", help="Use this skill instead of choosing one.")
    ] = None,
    budget_usd: Annotated[
        float | None, typer.Option("--budget-usd", min=0.0, help="Cost limit for this run.")
    ] = None,
    decision_provider: Annotated[
        str | None, typer.Option("--decision-provider", help="rules | jev (default from config).")
    ] = None,
    max_parallel: Annotated[
        int | None,
        typer.Option(
            "--max-parallel", min=1, help="Concurrent tasks (default: execution.max_parallel)."
        ),
    ] = None,
    scope: Annotated[
        list[str] | None,
        typer.Option("--scope", help="Glob the agent may change; repeatable. Default: everything."),
    ] = None,
    allow_dirty: Annotated[
        bool, typer.Option("--allow-dirty", help="Snapshot uncommitted tracked changes.")
    ] = False,
    keep_worktrees: Annotated[
        bool, typer.Option("--keep-worktrees", help="Do not delete the attempt worktree.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Run a goal with one agent in an isolated worktree; results land on branch aix/run/<id>."""
    from aix.core.orchestrator.single import SingleTaskRequest, run_single_task
    from aix.domain.errors import ConfigError, NoEligibleAgent, ToolFailure
    from aix.store.db import EventStore

    root = project.resolve()
    resolved = load_or_exit(root)
    if not (root / ".aix" / "config.yaml").exists():
        fail("not initialized: run `aix init` first")
    registry = registry_or_exit(resolved)
    if plan_only:
        _plan_only(root, goal, agent, skill, allow_dirty, as_json, resolved, registry)
        return
    if agent is None:
        config = resolved.config
        if budget_usd is not None:
            config = config.model_copy(
                update={
                    "budget": config.budget.model_copy(update={"max_cost_usd_per_run": budget_usd})
                }
            )
        if decision_provider is not None:
            if decision_provider not in ("rules", "jev"):
                fail("--decision-provider must be 'rules' or 'jev'")
            config = config.model_copy(
                update={
                    "decision": config.decision.model_copy(update={"provider": decision_provider})
                }
            )
        _routed(
            root,
            goal,
            skill,
            max_parallel,
            allow_dirty,
            keep_worktrees,
            as_json,
            replace(resolved, config=config),
            registry,
        )
        return
    request = SingleTaskRequest(
        project_root=root,
        goal=goal,
        agent_id=agent,
        file_scope=list(scope) if scope else ["**"],
        allow_dirty=allow_dirty,
        keep_worktrees=keep_worktrees,
    )

    async def _go():  # type: ignore[no-untyped-def]
        store = await EventStore.open(root / ".aix" / "aix.db")
        try:
            return await run_single_task(
                request, registry=registry, store=store, config=resolved.config
            )
        finally:
            await store.close()

    try:
        result = anyio.run(_go)
    except ConfigError as exc:
        fail(str(exc))
    except NoEligibleAgent as exc:
        fail(str(exc), EXIT_ENVIRONMENT)
    except ToolFailure as exc:
        reason = exc.details.get("reason")
        fail(str(exc), EXIT_ENVIRONMENT if reason == "not_git" else 2)

    if as_json:
        doc = asdict(result)
        doc["status"] = result.status.value
        doc["failure"] = result.failure.value if result.failure else None
        doc["diff"] = result.diff.model_dump(mode="json") if result.diff else None
        doc["usage"] = result.usage.model_dump(mode="json")
        doc["exit_code"] = result.exit_code
        typer.echo(json.dumps(doc, indent=2))
    else:
        _print_summary(result, goal)
    raise typer.Exit(result.exit_code)


def _print_summary(result, goal: str) -> None:  # type: ignore[no-untyped-def]
    """Interim summary (the full §23.3 layout arrives with reports in M7.3)."""
    done = result.status.value == "completed"
    typer.echo(f"RUN {result.run_id} {result.status.value.upper()}    branch: {result.branch}")
    typer.echo(f"Goal: {goal}")
    typer.echo("")
    typer.echo(f"Tasks        {1 if done else 0}/1 completed")
    typer.echo(f"Agent        {result.agent_id}")
    if result.diff is not None:
        d = result.diff
        typer.echo(f"Changes      {d.files_changed} files (+{d.lines_added} -{d.lines_removed})")
    if result.failure is not None:
        typer.echo(f"Failure      {result.failure.value}")
    cost = f"${result.usage.cost_usd:.4f}" if result.usage.cost_usd is not None else "n/a"
    typer.echo(f"Cost         {cost}   Duration {result.duration_ms / 1000:.1f}s")
    if result.claim:
        typer.echo(f"Agent claim (unverified): {result.claim[:300]}")
    if done:
        typer.echo("")
        typer.echo(f"Next: git merge {result.branch}")


def _plan_only(root, goal, agent, skill, allow_dirty, as_json, resolved, registry) -> None:  # type: ignore[no-untyped-def]
    """Plan without executing: create the run, record the plan, print it."""
    from aix.cli.plan import plan_document, print_plan
    from aix.core.orchestrator.plan import PlanRunRequest, plan_run
    from aix.domain.errors import ConfigError, ToolFailure
    from aix.store.db import EventStore

    request = PlanRunRequest(
        project_root=root, goal=goal, skill=skill, planner_agent=agent, allow_dirty=allow_dirty
    )

    async def _go():  # type: ignore[no-untyped-def]
        store = await EventStore.open(root / ".aix" / "aix.db")
        try:
            result = await plan_run(request, registry=registry, store=store, config=resolved.config)
            run = await store.get_run(result.run_id)
            return result, run
        finally:
            await store.close()

    try:
        result, run = anyio.run(_go)
    except ConfigError as exc:
        fail(str(exc))
    except ToolFailure as exc:
        reason = exc.details.get("reason")
        fail(str(exc), EXIT_ENVIRONMENT if reason == "not_git" else 2)
    doc = plan_document(
        run_id=result.run_id,
        status=run.status.value if run else "planned",
        goal=goal,
        intent=result.intent,
        planner=result.planner,
        warnings=result.warnings,
        tasks=list(result.graph.tasks),
    )
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
    else:
        print_plan(doc)
        typer.echo("")
        typer.echo(f"Plan recorded. Show again with: aix plan show {result.run_id}")


def _routed(
    root, goal, skill, max_parallel, allow_dirty, keep_worktrees, as_json, resolved, registry
) -> None:  # type: ignore[no-untyped-def]
    """Plan, route and execute a goal across agents; Ctrl-C cancels gracefully."""
    import signal

    from aix.core.orchestrator.executor import RunOutcome, RunRequest, execute_run
    from aix.domain.errors import ConfigError, ToolFailure
    from aix.store.db import EventStore

    request = RunRequest(
        project_root=root,
        goal=goal,
        skill=skill,
        allow_dirty=allow_dirty,
        keep_worktrees=keep_worktrees,
        max_parallel=max_parallel,
    )

    async def _go() -> tuple[RunOutcome, str | None]:
        cancel = anyio.Event()
        outcome: RunOutcome | None = None
        store = await EventStore.open(root / ".aix" / "aix.db")
        try:
            async with anyio.create_task_group() as tg:

                async def watch_signals() -> None:
                    with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                        async for _ in signals:
                            cancel.set()

                tg.start_soon(watch_signals)
                outcome = await execute_run(
                    request, registry=registry, store=store, config=resolved.config, cancel=cancel
                )
                tg.cancel_scope.cancel()
            assert outcome is not None
            summary = await _summary(store, root, outcome.run_id)
        finally:
            await store.close()
        assert outcome is not None
        return outcome, summary

    try:
        outcome, summary = anyio.run(_go)
    except ConfigError as exc:
        fail(str(exc))
    except ToolFailure as exc:
        reason = exc.details.get("reason")
        fail(str(exc), EXIT_ENVIRONMENT if reason == "not_git" else 2)

    if as_json:
        doc = asdict(outcome)
        doc["status"] = outcome.status.value
        doc["failure"] = outcome.failure.value if outcome.failure else None
        doc["usage"] = outcome.usage.model_dump(mode="json")
        doc["exit_code"] = outcome.exit_code
        for t in doc["tasks"]:
            t["status"] = t["status"].value
            t["failure"] = t["failure"].value if t["failure"] else None
        typer.echo(json.dumps(doc, indent=2))
    elif summary is not None:
        typer.echo(summary)
        for approval_id in outcome.pending_approvals:
            typer.echo(
                f"Waiting      approval needed: aix approve {approval_id}  (or: aix deny ...)"
            )
    else:
        print_outcome(outcome, goal)
    raise typer.Exit(outcome.exit_code)


async def _summary(store, root: Path, run_id: str) -> str | None:  # type: ignore[no-untyped-def]
    """The §23.3 block, or ``None`` when it cannot be built (the interim summary is used)."""
    from aix.artifacts.report import artifact_names, build_report, render_summary

    try:
        report = await build_report(
            store, run_id, artifact_names=await artifact_names(store, root, run_id)
        )
    except (LookupError, OSError, ValueError):
        return None
    return render_summary(report).rstrip("\n")


def print_outcome(outcome, goal: str) -> None:  # type: ignore[no-untyped-def]
    """Interim summary (the full §23.3 layout arrives with reports in M7.3)."""
    tasks = outcome.tasks
    done = sum(1 for t in tasks if t.status.value == "completed")
    typer.echo(f"RUN {outcome.run_id} {outcome.status.value.upper()}    branch: {outcome.branch}")
    typer.echo(f"Goal: {goal}")
    typer.echo("")
    typer.echo(f"Tasks        {done}/{len(tasks)} completed")
    agents = sorted({t.agent_id for t in tasks if t.agent_id})
    typer.echo(f"Agents       {', '.join(agents) or 'none'}")
    typer.echo(f"Planner      {outcome.planner}")
    for t in tasks:
        extra = f"  [{t.failure.value}]" if t.failure else ""
        who = f"  ({t.agent_id})" if t.agent_id else ""
        typer.echo(f"  - {t.type:<16} {t.status.value:<10}{who}{extra}")
    if outcome.failure is not None:
        typer.echo(f"Failure      {outcome.failure.value}")
    cost = f"${outcome.usage.cost_usd:.4f}" if outcome.usage.cost_usd is not None else "n/a"
    typer.echo(f"Cost         {cost}   Duration {outcome.duration_ms / 1000:.1f}s")
    for w in outcome.warnings:
        typer.echo(f"Note         {w}")
    for approval_id in outcome.pending_approvals:
        typer.echo(f"Waiting      approval needed: aix approve {approval_id}  (or: aix deny ...)")
    if outcome.status.value == "completed":
        typer.echo("")
        typer.echo(f"Next: git merge {outcome.branch}")
