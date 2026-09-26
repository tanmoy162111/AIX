"""`aix artifact list|show|export|verify` (PLAYBOOK §21.3, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import anyio
import typer

from aix.cli.common import fail, load_or_exit

artifact_app = typer.Typer(
    name="artifact", help="Inspect, export and verify artifacts.", no_args_is_help=True
)
_Project = Annotated[Path, typer.Option("--project", help="Project root.")]
_Json = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _db(root: Path) -> Path:
    load_or_exit(root)
    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")
    return db


@artifact_app.command("list")
def list_(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    as_json: _Json = False,
    project: _Project = Path(),
) -> None:
    """List a run's artifacts with names, types, sizes and hashes."""
    from aix.artifacts.report import artifact_names
    from aix.store.db import EventStore

    root = project.resolve()
    db = _db(root)

    async def _go() -> list[dict[str, Any]] | None:
        store = await EventStore.open(db)
        try:
            if await store.get_run(run_id) is None:
                return None
            arts = await store.get_artifacts(run_id)
            names = await artifact_names(store, root, run_id)
            by_sha: dict[str, str] = {}
            if arts and names:
                manifest = next((a for a in reversed(arts) if a.type.value == "manifest"), None)
                if manifest:
                    from aix.artifacts.store import ObjectStore

                    blob = await ObjectStore(root / ".aix" / "artifacts" / "objects").get(
                        manifest.sha256
                    )
                    by_sha = {e["artifact_id"]: e["name"] for e in json.loads(blob)["artifacts"]}
            return [
                {
                    "id": a.id,
                    "name": by_sha.get(
                        a.id, "manifest.json" if a.type.value == "manifest" else "-"
                    ),
                    "type": a.type.value,
                    "size": a.size,
                    "sha256": a.sha256,
                }
                for a in arts
            ]
        finally:
            await store.close()

    rows = anyio.run(_go)
    if rows is None:
        fail(f"unknown run {run_id!r}")
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    from rich.console import Console
    from rich.table import Table

    table = Table("id", "name", "type", "size", "sha256")
    for r in rows:
        table.add_row(r["id"], r["name"], r["type"], str(r["size"]), r["sha256"][:12])
    Console(width=180).print(table)


@artifact_app.command("show")
def show(
    artifact_id: Annotated[str, typer.Argument(help="Artifact id.")],
    as_json: _Json = False,
    project: _Project = Path(),
) -> None:
    """Print an artifact's provenance and, for text types, its content."""
    from aix.artifacts.store import ObjectStore
    from aix.store.db import EventStore

    root = project.resolve()
    db = _db(root)

    async def _go() -> tuple[Any, bytes] | None:
        store = await EventStore.open(db)
        try:
            art = await store.get_artifact(artifact_id)
            if art is None:
                return None
            return art, await ObjectStore(root / ".aix" / "artifacts" / "objects").get(art.sha256)
        finally:
            await store.close()

    try:
        found = anyio.run(_go)
    except FileNotFoundError:
        fail("the artifact's blob is missing from the object store")
    if found is None:
        fail(f"unknown artifact {artifact_id!r}")
    art, data = found
    if as_json:
        typer.echo(json.dumps(art.model_dump(mode="json"), indent=2))
        return
    typer.echo(json.dumps(art.model_dump(mode="json"), indent=2))
    if art.media_type.startswith(("text/", "application/json", "application/xml")):
        typer.echo("")
        typer.echo(data.decode("utf-8", errors="replace"))


@artifact_app.command("export")
def export(
    run_id: Annotated[str, typer.Argument(help="Run id.")],
    bundle: Annotated[
        bool, typer.Option("--bundle", help="Write a verifiable zip bundle.")
    ] = False,
    as_json: _Json = False,
    project: _Project = Path(),
) -> None:
    """Export a finished run's artifacts (`--bundle` writes .aix/artifacts/bundles/<run>.zip)."""
    from aix.artifacts.bundle import BundleError, export_bundle
    from aix.store.db import EventStore

    if not bundle:
        fail("nothing to do: pass --bundle")
    root = project.resolve()
    db = _db(root)

    async def _go() -> Path:
        store = await EventStore.open(db)
        try:
            if await store.get_run(run_id) is None:
                fail(f"unknown run {run_id!r}")
            return await export_bundle(store, root, run_id)
        finally:
            await store.close()

    try:
        path = anyio.run(_go)
    except BundleError as exc:
        fail(str(exc), 1)
    if as_json:
        typer.echo(json.dumps({"run_id": run_id, "bundle": str(path)}))
    else:
        typer.echo(f"Bundle written: {path}")
        typer.echo(f"Verify with: aix artifact verify {path}")


@artifact_app.command("verify")
def verify_(
    bundle: Annotated[Path, typer.Argument(help="Path to a bundle zip.")],
    as_json: _Json = False,
) -> None:
    """Re-hash every file in a bundle and check it against its manifest (exit 1 on any problem)."""
    from aix.artifacts.bundle import verify_bundle

    result = verify_bundle(bundle)
    if as_json:
        typer.echo(
            json.dumps({"ok": result.ok, "files": result.files, "problems": result.problems})
        )
    elif result.ok:
        typer.echo(f"OK: {result.files} files match the manifest")
    else:
        typer.echo(f"FAILED: {len(result.problems)} problem(s)")
        for p in result.problems:
            typer.echo(f"  - {p}")
    raise typer.Exit(0 if result.ok else 1)
