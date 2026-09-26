"""`aix cancel <run>` (PLAYBOOK §14.1, §23.1)."""

from __future__ import annotations

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
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Cancel a run: live agents are stopped, pending tasks cancelled, partial results kept."""
    from aix.core.orchestrator.cancel import (
        DIRECT_CANCEL_STATUSES,
        TERMINAL_RUN_STATUSES,
        force_cancel,
    )
    from aix.core.orchestrator.executor import cancel_marker
    from aix.store.db import EventStore

    root = project.resolve()
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")

    async def _go() -> tuple[str, str]:
        store = await EventStore.open(db)
        try:
            run = await store.get_run(run_id)
            if run is None:
                return "unknown", ""
            if run.status in TERMINAL_RUN_STATUSES:
                return "already", run.status.value
            if run.status in DIRECT_CANCEL_STATUSES:
                status = await force_cancel(store, run_id, reason="cancelled by user")
                return "cancelled", status.value
            marker = cancel_marker(root, run_id)
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("cancel\n", encoding="utf-8")
            with anyio.move_on_after(timeout):
                while True:
                    current = await store.get_run(run_id)
                    if current is not None and current.status in TERMINAL_RUN_STATUSES:
                        done = current.status.value
                        return ("cancelled" if done == "cancelled" else "already"), done
                    await anyio.sleep(0.2)
            if force:
                status = await force_cancel(store, run_id, reason="cancelled by user (forced)")
                return "cancelled", status.value
            return "no_response", run.status.value
        finally:
            await store.close()

    kind, detail = anyio.run(_go)
    if kind == "unknown":
        fail(f"unknown run {run_id!r}")
    if kind == "already":
        typer.echo(f"Run {run_id} is already {detail}.")
    elif kind == "cancelled":
        typer.echo(f"Run {run_id} cancelled.")
    else:
        fail(
            f"cancellation requested but the run is still {detail}; no live orchestrator "
            "responded. If it crashed, use --force.",
            EXIT_ENVIRONMENT,
        )
