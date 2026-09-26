"""`aix cancel <run>` (PLAYBOOK §14.1, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit


def cancel(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    force: Annotated[
        bool,
        typer.Option("--force", help="Mark the run cancelled even if no orchestrator responds."),
    ] = False,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds to wait for a live orchestrator.")
    ] = 15.0,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Cancel a run: live agents are stopped, pending tasks cancelled, partial results kept."""
    from aix.core.orchestrator.cancel import request_cancel
    from aix.store.db import EventStore

    root = project.resolve()
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")

    async def _go() -> tuple[str, str]:
        store = await EventStore.open(db)
        try:
            result = await request_cancel(store, root, run_id, wait_s=timeout, force=force)
        finally:
            await store.close()
        return result.kind, result.status

    kind, detail = anyio.run(_go)
    if kind == "unknown":
        fail(f"unknown run {run_id!r}")
    if kind in ("already", "cancelled") and as_json:
        typer.echo(json.dumps({"run_id": run_id, "result": kind, "status": detail}))
    elif kind == "already":
        typer.echo(f"Run {run_id} is already {detail}.")
    elif kind == "cancelled":
        typer.echo(f"Run {run_id} cancelled.")
    else:
        fail(
            f"cancellation requested but the run is still {detail}; no live orchestrator "
            "responded. If it crashed, use --force.",
            EXIT_ENVIRONMENT,
        )
