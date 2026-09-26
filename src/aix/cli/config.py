"""`aix config ...` commands (PLAYBOOK §9, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

config_app = typer.Typer(name="config", help="Inspect configuration.", no_args_is_help=True)


@config_app.command("show")
def show(
    resolved: Annotated[
        bool, typer.Option("--resolved", help="Show every key with the layer that set it.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Print the effective configuration."""
    import yaml
    from rich.console import Console
    from rich.table import Table

    from aix.config.loader import load_config
    from aix.domain.errors import ConfigError

    try:
        result = load_config(project.resolve())
    except ConfigError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from exc

    if not resolved:
        dumped = result.config.model_dump(mode="json")
        typer.echo(
            json.dumps(dumped, indent=2) if as_json else yaml.safe_dump(dumped, sort_keys=False)
        )
        return

    rows = result.rows()
    if as_json:
        typer.echo(json.dumps([{"key": k, "value": v, "source": s} for k, v, s in rows], indent=2))
        return
    table = Table("key", "value", "source", show_lines=False)
    for key, value, source in rows:
        table.add_row(key, json.dumps(value), source)
    Console(width=200).print(table)
