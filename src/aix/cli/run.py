"""`aix run` (PLAYBOOK §23.1). M2 scope: one goal, one explicit agent, one attempt."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit, registry_or_exit


def run(
    goal: Annotated[str, typer.Argument(help="What you want done.")],
    agent: Annotated[str, typer.Option("--agent", help="Agent id (routing arrives in M3).")],
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
