"""`aix trace <run>` and `aix logs <run>` (PLAYBOOK §22, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import anyio
import typer

from aix.cli.common import fail, load_or_exit

if TYPE_CHECKING:
    from aix.observability.trace import TraceNode

_Project = Annotated[Path, typer.Option("--project", help="Project root.")]
_Json = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
_STYLE = {"completed": "green", "passed": "green", "accept": "green", "failed": "red",
          "cancelled": "yellow", "warning": "yellow", "skipped": "yellow"}  # fmt: skip


def _db(root: Path) -> Path:
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")
    return db


def _fmt_ms(ms: int | None) -> str:
    if ms is None:
        return ""
    return f"{ms}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _add(tree: Any, node: TraceNode) -> None:
    style = _STYLE.get(node.status.split(" ")[0], "")
    dur = f"  {_fmt_ms(node.duration_ms)}" if node.duration_ms is not None else ""
    text = f"[bold]{node.kind}[/bold] {node.label}  [{style or 'white'}]{node.status}[/]{dur}"
    branch = tree.add(text)
    for child in node.children:
        _add(branch, child)


def trace(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    as_json: _Json = False,
    project: _Project = Path(),
) -> None:
    """Show a run as a tree of tasks, attempts, checks and decisions with durations."""
    from rich.console import Console
    from rich.markup import escape
    from rich.tree import Tree

    from aix.observability.trace import build_trace
    from aix.store.db import EventStore

    db = _db(project.resolve())

    async def _go() -> TraceNode | None:
        store = await EventStore.open(db)
        try:
            return await build_trace(store, run_id)
        except LookupError:
            return None
        finally:
            await store.close()

    root = anyio.run(_go)
    if root is None:
        fail(f"unknown run {run_id!r}")
    if as_json:
        typer.echo(json.dumps(root.to_dict(), indent=2))
        return
    tree = Tree(
        f"[bold]run[/bold] {root.id}  {escape(root.label)}  {root.status}  "
        f"{_fmt_ms(root.duration_ms)}"
    )
    for child in root.children:
        _add(tree, child)
    Console(width=160, highlight=False).print(tree)


def _line(e: Any) -> str:
    who = e.task_id or ""
    body = ""
    payload = e.payload
    text = getattr(payload, "text", None)
    if isinstance(text, str):
        body = text
    elif e.type == "task.state_changed":
        body = f"{payload.from_status.value} -> {payload.to_status.value}"
    elif e.type == "agent.selected":
        body = f"{payload.agent_id}"
    return f"{e.ts.strftime('%H:%M:%S')} {e.type:<24} {who[-8:]:<8} {body}".rstrip()


def logs(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    task: Annotated[str | None, typer.Option("--task", help="Only events of this task id.")] = None,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing until the run ends.")
    ] = False,
    as_json: _Json = False,
    project: _Project = Path(),
) -> None:
    """Print a run's events (agent milestones, state changes, decisions); `--json` emits JSONL."""
    from aix.domain.enums import RUN_TERMINAL
    from aix.store.db import EventStore

    db = _db(project.resolve())

    def emit(e: Any) -> None:
        if as_json:
            typer.echo(
                json.dumps(
                    {
                        "seq": e.seq,
                        "ts": e.ts.isoformat(),
                        "type": e.type,
                        "task_id": e.task_id,
                        "attempt_id": e.attempt_id,
                        "payload": e.payload.model_dump(mode="json"),
                    }
                )
            )
        else:
            typer.echo(_line(e))

    async def _go() -> bool:
        store = await EventStore.open(db)
        try:
            after = 0
            while True:
                run = await store.get_run(run_id)  # read status first so the last fetch drains
                if run is None:
                    return False
                for e in await store.events(run_id=run_id, after_seq=after):
                    after = e.seq
                    if not task or e.task_id == task:
                        emit(e)
                if not follow or run.status in RUN_TERMINAL:
                    return True
                await anyio.sleep(0.5)
        finally:
            await store.close()

    if not anyio.run(_go):
        fail(f"unknown run {run_id!r}")
