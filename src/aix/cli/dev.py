"""`aix dev ...` maintenance commands (PLAYBOOK §23.1)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from pydantic import BaseModel

dev_app = typer.Typer(name="dev", help="Developer and maintenance commands.", no_args_is_help=True)


def all_schema_models() -> dict[str, type[BaseModel]]:
    """Every model whose JSON Schema is committed under ``schemas/``."""
    from aix.domain.schemas import collect_domain_models
    from aix.store.events import EVENT_PAYLOADS

    models: dict[str, type[BaseModel]] = dict(collect_domain_models())
    for event_type, payload in EVENT_PAYLOADS.items():
        models[f"event_{event_type.replace('.', '_')}"] = payload
    return dict(sorted(models.items()))


@dev_app.command("export-schemas")
def export_schemas(
    out: Annotated[Path, typer.Option("--out", help="Directory to write schemas into.")] = Path(
        "schemas"
    ),
) -> None:
    """Write one JSON Schema per model to ``schemas/<name>.json`` and remove stale files."""
    from aix.domain.schemas import render_schema

    models = all_schema_models()
    out.mkdir(parents=True, exist_ok=True)
    for name, model in models.items():
        (out / f"{name}.json").write_text(render_schema(model))
    for stale in out.glob("*.json"):
        if stale.stem not in models:
            stale.unlink()
    typer.echo(f"exported {len(models)} schemas to {out}")
