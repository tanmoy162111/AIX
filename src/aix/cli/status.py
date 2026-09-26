"""`aix status [<run>]`: task table for a run, refreshing while it is active (PLAYBOOK §23.1)."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import anyio
import typer

from aix.cli.common import fail, load_or_exit

if TYPE_CHECKING:
    from rich.table import Table

    from aix.core.orchestrator.status import RunSnapshot

_STYLE = {
    "completed": "green",
    "failed": "red",
    "cancelled": "yellow",
    "blocked": "yellow",
    "running": "cyan",
    "verifying": "cyan",
    "deciding": "cyan",
}


def snapshot_document(snap: RunSnapshot) -> dict[str, Any]:
    """The machine-readable snapshot (``--json``)."""
    doc = asdict(snap)
    doc["status"] = snap.status.value
    doc["counts"] = snap.counts
    for task in doc["tasks"]:
        task["status"] = task["status"].value
    return doc


def _header(snap: RunSnapshot) -> str:
    done = snap.counts.get("completed", 0)
    return (
        f"RUN {snap.run_id} {snap.status.value.upper()}    "
        f"tasks {done}/{len(snap.tasks)}    planner {snap.planner}    branch {snap.branch}"
    )


def build_table(snap: RunSnapshot) -> Table:
    """One row per task."""
    from rich.table import Table

    table = Table("#", "type", "status", "agent", "tries", "depends", "note")
    for t in snap.tasks:
        style = _STYLE.get(t.status.value, "")
        table.add_row(
            str(t.number),
            t.type,
            f"[{style}]{t.status.value}[/{style}]" if style else t.status.value,
            t.agent or "-",
            str(t.attempts),
            ",".join(str(d) for d in t.depends_on) or "-",
            t.failure or "",
        )
    return table


def status(
    run_id: Annotated[str | None, typer.Argument(help="Run id (default: the latest run).")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable snapshot.")] = False,
    watch: Annotated[
        bool | None,
        typer.Option("--watch/--no-watch", help="Refresh until the run ends (default: on a TTY)."),
    ] = None,
    interval: Annotated[
        float, typer.Option("--interval", min=0.05, help="Seconds between polls.")
    ] = 1.0,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Show the tasks of a run; while it is active the table refreshes until it finishes."""
    from rich.console import Console

    from aix.core.orchestrator.status import latest_run_id, snapshot
    from aix.domain.errors import ConfigError
    from aix.store.db import EventStore

    root = project.resolve()
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")
    tty = sys.stdout.isatty()
    follow = tty if watch is None else watch
    if as_json:
        follow = False

    async def _go() -> tuple[RunSnapshot, bool]:
        """The snapshot and whether it was already drawn while watching."""
        store = await EventStore.open(db)
        try:
            rid = run_id or await latest_run_id(store)
            if rid is None:
                raise ConfigError("no runs recorded yet")
            snap = await snapshot(store, rid)
            if as_json or not follow or snap.terminal:
                return snap, False
            return await _watch(store, rid, snap, interval, tty, Console()), True
        finally:
            await store.close()

    try:
        snap, drawn = anyio.run(_go)
    except ConfigError as exc:
        fail(str(exc))
    if as_json:
        typer.echo(json.dumps(snapshot_document(snap), indent=2))
    elif not drawn:
        _print(snap)


def _print(snap: RunSnapshot) -> None:
    from rich.console import Console

    typer.echo(_header(snap))
    typer.echo(f"Goal: {snap.goal}")
    typer.echo("")
    Console(width=160).print(build_table(snap))


async def _watch(store, rid, first, interval, tty, console):  # type: ignore[no-untyped-def]
    """Poll until the run is terminal. TTY: redraw in place; otherwise print each changed frame."""
    from rich.console import Group
    from rich.live import Live
    from rich.text import Text

    from aix.core.orchestrator.status import snapshot

    def frame(s):  # type: ignore[no-untyped-def]
        return Group(Text(_header(s)), Text(f"Goal: {s.goal}"), Text(""), build_table(s))

    snap = first
    if tty:
        with Live(frame(snap), console=console, refresh_per_second=4) as live:
            while not snap.terminal:
                await anyio.sleep(interval)
                snap = await snapshot(store, rid)
                live.update(frame(snap))
        return snap
    last = None
    while True:
        key = (snap.status, [(t.status, t.attempts, t.agent) for t in snap.tasks])
        if key != last:
            _print(snap)
            typer.echo("")
            last = key
        if snap.terminal:
            return snap
        await anyio.sleep(interval)
        snap = await snapshot(store, rid)
