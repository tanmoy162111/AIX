"""`aix init` (PLAYBOOK §23.1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

GITIGNORE_HEADER = "# aix runtime files"
GITIGNORE_ENTRIES = (
    ".aix/worktrees/",
    ".aix/runs/",
    ".aix/artifacts/",
    ".aix/aix.db",
    ".aix/aix.db-wal",
    ".aix/aix.db-shm",
)


def config_template() -> str:
    """A fully commented-out config documenting every default (valid, empty YAML)."""
    import yaml

    from aix.config.schema import AixConfig

    body = yaml.safe_dump(AixConfig().model_dump(mode="json"), sort_keys=False)
    commented = "\n".join(f"# {line}" if line else "#" for line in body.splitlines())
    return (
        "# aix project configuration (PLAYBOOK §9).\n"
        "# Below are the built-in defaults, commented out; uncomment a key to override it.\n"
        "# Secrets never go here: use environment variables.\n"
        f"{commented}\n"
    )


def ensure_gitignore(path: Path) -> list[str]:
    """Append missing aix entries to ``path`` (creating it); returns the entries that were added."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [e for e in GITIGNORE_ENTRIES if e not in present]
    if not missing:
        return []
    prefix = existing if existing == "" or existing.endswith("\n") else existing + "\n"
    header = "" if GITIGNORE_HEADER in present else f"{GITIGNORE_HEADER}\n"
    path.write_text(prefix + header + "\n".join(missing) + "\n", encoding="utf-8")
    return missing


def init(
    force: Annotated[bool, typer.Option("--force", help="Rewrite .aix/config.yaml.")] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Create .aix/, a default config and database, and .gitignore entries; detect the toolchain."""
    import anyio

    from aix.store.db import EventStore
    from aix.verification.detect import detect_toolchain

    root = project.resolve()
    aix_dir = root / ".aix"
    aix_dir.mkdir(parents=True, exist_ok=True)

    config_path = aix_dir / "config.yaml"
    already = config_path.exists() and not force
    if not already:
        config_path.write_text(config_template(), encoding="utf-8")

    async def _touch_db() -> None:
        store = await EventStore.open(aix_dir / "aix.db")
        await store.close()

    anyio.run(_touch_db)
    added = ensure_gitignore(root / ".gitignore")
    toolchain = detect_toolchain(root)

    warnings: list[str] = []
    if not toolchain.is_git_repo:
        warnings.append("not a git repository: `aix run` needs one (run `git init`)")

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "project": str(root),
                    "config": str(config_path),
                    "database": str(aix_dir / "aix.db"),
                    "created_config": not already,
                    "gitignore_added": added,
                    "toolchain": toolchain.model_dump(mode="json"),
                    "warnings": warnings,
                },
                indent=2,
            )
        )
        return

    typer.echo(f"aix initialized in {root}")
    typer.echo(
        f"  config: {config_path} (already initialized, kept)"
        if already
        else f"  config: {config_path}"
    )
    typer.echo(f"  database: {aix_dir / 'aix.db'}")
    if added:
        typer.echo(f"  .gitignore: added {', '.join(added)}")
    eco = ", ".join(toolchain.ecosystems) or "none detected"
    typer.echo(f"  toolchain: {eco}")
    for w in warnings:
        typer.echo(f"warning: {w}")
