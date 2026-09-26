"""`aix stats agents` (PLAYBOOK §22): observed agent history. No global ranking is shown."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import anyio
import typer

from aix.cli.common import fail, load_or_exit

stats_app = typer.Typer(name="stats", help="Observed statistics.", no_args_is_help=True)


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


@stats_app.command("agents")
def agents(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Per agent, model and task type: acceptance, verification, retries, cost, latency."""
    from aix.store.db import EventStore

    root = project.resolve()
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")

    async def _go():  # type: ignore[no-untyped-def]
        store = await EventStore.open(db)
        try:
            return await store.agent_stats()
        finally:
            await store.close()

    rows = anyio.run(_go)
    if as_json:
        docs = []
        for r in rows:
            d = r.model_dump(mode="json")
            d.update(
                accept_rate=r.accept_rate, verification_pass_rate=r.verification_pass_rate,
                retry_rate=r.retry_rate, human_rate=r.human_rate,
            )  # fmt: skip
            docs.append(d)
        typer.echo(json.dumps(docs, indent=2))
        return
    if not rows:
        typer.echo("No attempts recorded yet.")
        return
    from rich.console import Console
    from rich.table import Table

    table = Table(
        "agent", "model", "task type", "attempts", "accepted", "verified", "retry", "human",
        "mean cost", "p50", "p90",
    )  # fmt: skip
    for r in rows:
        table.add_row(
            r.agent_id, r.model or "-", r.task_type, str(r.attempts), _pct(r.accept_rate),
            _pct(r.verification_pass_rate), _pct(r.retry_rate), _pct(r.human_rate),
            "-" if r.mean_cost_usd is None else f"${r.mean_cost_usd:.4f}",
            "-" if r.p50_latency_s is None else f"{r.p50_latency_s:.1f}s",
            "-" if r.p90_latency_s is None else f"{r.p90_latency_s:.1f}s",
        )  # fmt: skip
    Console(width=180).print(table)
    typer.echo(
        "Rates are shares of attempts. Small samples are noisy; there is no overall ranking."
    )
