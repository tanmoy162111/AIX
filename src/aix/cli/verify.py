"""`aix verify` and `aix review` (PLAYBOOK §17, §23.1)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit, registry_or_exit

if TYPE_CHECKING:
    from aix.domain.verification import Check

PathOpt = Annotated[Path, typer.Option("--path", help="Project directory.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
BaseOpt = Annotated[
    str | None, typer.Option("--base", help="Compare against this git ref (default: HEAD).")
]
_STYLE = {
    "passed": "green",
    "failed": "red",
    "error": "red",
    "warning": "yellow",
    "skipped": "yellow",
}


def _table(checks: list[Check]):  # type: ignore[no-untyped-def]
    from rich.table import Table

    table = Table("check", "status", "required", "summary")
    for c in checks:
        style = _STYLE.get(c.status, "")
        status = f"[{style}]{c.status}[/{style}]" if style else c.status
        table.add_row(c.kind.value, status, "yes" if c.required else "no", c.summary)
    return table


def verify(
    path: PathOpt = Path(),
    as_json: JsonOpt = False,
    base: BaseOpt = None,
) -> None:
    """Run verification on the current tree (no agents): build, tests, lint, security, policy."""
    from rich.console import Console

    from aix.domain.enums import CheckKind as K
    from aix.domain.ids import IdPrefix, new_id
    from aix.domain.tasks import VerificationSpec
    from aix.verification.engine import run_verification
    from aix.verification.tree import working_tree_change

    root = path.resolve()
    if not root.is_dir():
        fail(f"not a directory: {root}")
    resolved = load_or_exit(root)
    cfg = resolved.config
    spec = VerificationSpec(
        required=list(cfg.verification.required_default), optional=[K.TYPECHECK]
    )
    attempt_id = new_id(IdPrefix.ATTEMPT)
    out_root = root / ".aix" / "verify" if (root / ".aix").is_dir() else None

    async def _go():  # type: ignore[no-untyped-def]
        change = await working_tree_change(root, base)
        out_dir = (
            (out_root / attempt_id) if out_root else Path(tempfile.mkdtemp(prefix="aix-verify-"))
        )
        report = await run_verification(
            root, attempt_id, spec, cfg, patch=change.patch, changed_paths=change.paths,
            file_scope=["**"], out_dir=out_dir,
        )  # fmt: skip
        return report, change

    report, change = anyio.run(_go)
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(f"VERIFY {report.overall.upper()}    {root}")
        if change.is_git:
            typer.echo(f"Changed files: {len(change.paths)}")
        typer.echo("")
        Console(width=160).print(_table(report.checks))
        if report.overall == "incomplete":
            typer.echo("")
            typer.echo(
                "Incomplete: a required check could not run. Missing verification is not success."
            )
    raise typer.Exit(0 if report.overall in ("passed", "warning") else 1)


def review(
    path: PathOpt = Path(),
    agent: Annotated[str | None, typer.Option("--agent", help="Reviewer agent id.")] = None,
    base: BaseOpt = None,
    as_json: JsonOpt = False,
) -> None:
    """Independent AI review of the current diff (evidence about the diff, not a gate)."""
    from aix.domain.errors import ConfigError, NoEligibleAgent
    from aix.verification.ai_review import make_snapshot_runner, pick_reviewer, run_ai_review
    from aix.verification.tree import working_tree_change

    root = path.resolve()
    resolved = load_or_exit(root)
    registry = registry_or_exit(resolved)
    cfg = resolved.config

    async def _go():  # type: ignore[no-untyped-def]
        change = await working_tree_change(root, base)
        if not change.is_git:
            raise ConfigError("not a git repository")
        if change.empty:
            return None, None, None
        if agent is not None:
            registry.get(agent)  # raises ConfigError for unknown ids
            spec = await registry.probe(agent)
            if spec.health not in ("ready", "degraded"):
                raise NoEligibleAgent(f"agent {agent!r} is {spec.health}: {spec.health_reason}")
            reviewer = agent
        else:
            reviewer = pick_reviewer(await registry.probe_all(), set(), cfg.routing)
        override = cfg.agents.overrides.get(reviewer)
        runner = make_snapshot_runner(
            registry.get(reviewer), root,
            timeout_s=(override.timeout_s if override and override.timeout_s else None)
            or cfg.execution.attempt_timeout_s,
            model=override.model if override else None,
        )  # fmt: skip
        check = await run_ai_review(
            "Review the current uncommitted changes", change.patch, [], runner, reviewer_id=reviewer
        )
        return check, reviewer, change

    try:
        check, reviewer, _change = anyio.run(_go)
    except ConfigError as exc:
        fail(str(exc))
    except NoEligibleAgent as exc:
        fail(str(exc), EXIT_ENVIRONMENT)
    if check is None:
        if as_json:
            typer.echo(json.dumps({"status": "nothing_to_review"}))
        else:
            typer.echo("Nothing to review: the working tree has no changes.")
        raise typer.Exit(0)
    if as_json:
        doc = check.model_dump(mode="json")
        doc["reviewer"] = reviewer
        typer.echo(json.dumps(doc, indent=2))
    else:
        typer.echo(f"REVIEW {check.status.upper()}    reviewer: {reviewer}")
        typer.echo(check.summary)
    raise typer.Exit(0 if check.status in ("passed", "warning") else 1)
