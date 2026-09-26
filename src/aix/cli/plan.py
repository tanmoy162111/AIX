"""`aix plan show` and plan rendering shared with `aix run --plan-only` (PLAYBOOK §13.3, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import anyio
import typer

from aix.cli.common import fail, load_or_exit
from aix.domain.tasks import Intent, Task

plan_app = typer.Typer(name="plan", help="Inspect run plans.", no_args_is_help=True)


def plan_document(
    *,
    run_id: str,
    status: str,
    goal: str,
    intent: Intent | None,
    planner: str,
    warnings: list[str],
    tasks: list[Task],
) -> dict[str, Any]:
    """The machine-readable plan (`--json`), shared by `run --plan-only` and `plan show`."""
    return {
        "run_id": run_id,
        "status": status,
        "goal": goal,
        "intent": intent.model_dump(mode="json") if intent else None,
        "planner": planner,
        "warnings": warnings,
        "tasks": [t.model_dump(mode="json") for t in tasks],
    }


def print_plan(doc: dict[str, Any]) -> None:
    """Human rendering of :func:`plan_document`."""
    from rich.console import Console
    from rich.table import Table

    typer.echo(f"RUN {doc['run_id']} {str(doc['status']).upper()}")
    typer.echo(f"Goal: {doc['goal']}")
    intent = doc["intent"]
    if intent:
        typer.echo(f"Intent: {intent['kind']} · risk {intent['risk']}")
    typer.echo(f"Planner: {doc['planner']}")
    tasks: list[dict[str, Any]] = doc["tasks"]
    number = {t["id"]: i for i, t in enumerate(tasks, start=1)}
    table = Table("#", "type", "title", "depends", "scope", "checks", "skill")
    for i, t in enumerate(tasks, start=1):
        table.add_row(
            str(i),
            t["type"],
            t["title"],
            ",".join(str(number[d]) for d in t["depends_on"]) or "-",
            ", ".join(t["file_scope"]) or "read-only",
            ", ".join(t["verification"]["required"]) or "-",
            t["skill"] or "-",
        )
    typer.echo("")
    Console(width=160).print(table)
    if doc["warnings"]:
        typer.echo("")
        typer.echo("Notes:")
        for w in doc["warnings"]:
            typer.echo(f"  - {w}")


@plan_app.command("show")
def show(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Print the plan recorded for a run."""
    from aix.store.db import EventStore

    root = project.resolve()
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")

    async def _load() -> dict[str, Any] | None:
        store = await EventStore.open(db)
        try:
            run = await store.get_run(run_id)
            if run is None:
                return None
            planned = await store.events(run_id=run_id, types=["run.planned"])
            tasks = await store.get_tasks(run_id)
            from aix.store import events as ev

            payload = planned[-1].payload if planned else None
            planner = payload.planner if isinstance(payload, ev.RunPlannedPayload) else "-"
            warnings = payload.warnings if isinstance(payload, ev.RunPlannedPayload) else []
            return plan_document(
                run_id=run.id,
                status=run.status.value,
                goal=run.goal,
                intent=run.intent,
                planner=planner,
                warnings=warnings,
                tasks=tasks,
            )
        finally:
            await store.close()

    doc = anyio.run(_load)
    if doc is None:
        fail(f"unknown run {run_id!r}")
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
    else:
        print_plan(doc)
