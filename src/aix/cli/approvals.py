"""`aix approvals`, `aix approve`, `aix deny` (PLAYBOOK §20.4, §23.1)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Literal

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit, registry_or_exit

Channel = Literal["cli_tty", "api_token"]
ProjectOpt = Annotated[Path, typer.Option("--project", help="Project root.")]
TokenOpt = Annotated[
    str | None, typer.Option("--token", help="Approval token for non-interactive use.")
]


def _authenticate(approval_id: str, token: str | None) -> Channel:
    """Prove a human is deciding; returns the channel (``cli_tty`` or ``api_token``)."""
    from aix.security.approvals import (
        ApprovalRefused,
        confirm_tty,
        guard_agent_context,
        verify_token,
    )

    try:
        guard_agent_context()
        if token is not None:
            verify_token(token)
            return "api_token"
        confirm_tty(approval_id, isatty=sys.stdin.isatty() and sys.stdout.isatty())
        return "cli_tty"
    except ApprovalRefused as exc:
        fail(str(exc))


def _open_store(root: Path):  # type: ignore[no-untyped-def]
    from aix.store.db import EventStore

    db = root / ".aix" / "aix.db"
    if not db.exists():
        fail("not initialized: run `aix init` first")
    return EventStore.open(db)


def approvals(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    show_token: Annotated[
        bool, typer.Option("--show-token", help="Print the approval token (creates it).")
    ] = False,
    project: ProjectOpt = Path(),
) -> None:
    """List pending approvals."""
    from aix.security.approvals import ApprovalRefused, ensure_token, guard_agent_context

    if show_token:
        try:
            guard_agent_context()
        except ApprovalRefused as exc:
            fail(str(exc))
        typer.echo(ensure_token())
        return
    root = project.resolve()
    load_or_exit(root)

    async def _go():  # type: ignore[no-untyped-def]
        store = await _open_store(root)
        try:
            return await store.list_approvals("pending")
        finally:
            await store.close()

    pending = anyio.run(_go)
    if as_json:
        typer.echo(json.dumps([a.model_dump(mode="json") for a in pending], indent=2))
        return
    if not pending:
        typer.echo("No pending approvals.")
        return
    for a in pending:
        reasons = ", ".join(str(r) for r in a.scope.get("reasons", []))  # type: ignore[union-attr]
        typer.echo(f"{a.id}  {a.action}  subject={a.subject}  {reasons}")


def approve(
    ref: Annotated[str, typer.Argument(help="Approval id or the task/run id it concerns.")],
    token: TokenOpt = None,
    no_resume: Annotated[
        bool, typer.Option("--no-resume", help="Record the grant but do not continue the run.")
    ] = False,
    project: ProjectOpt = Path(),
) -> None:
    """Grant a pending approval (interactive confirmation or --token) and resume the run."""
    from aix.cli.run import print_outcome
    from aix.core.orchestrator.approvals import find_approval, resolve_approval
    from aix.core.orchestrator.executor import resume_run
    from aix.domain.errors import ConfigError, ToolFailure
    from aix.security.approvals import actor

    root = project.resolve()
    resolved = load_or_exit(root)

    async def _find():  # type: ignore[no-untyped-def]
        store = await _open_store(root)
        try:
            return await find_approval(store, ref)
        finally:
            await store.close()

    try:
        approval, _run_id = anyio.run(_find)
    except ConfigError as exc:
        fail(str(exc))
    channel = _authenticate(approval.id, token)
    registry = registry_or_exit(resolved)

    async def _go():  # type: ignore[no-untyped-def]
        store = await _open_store(root)
        try:
            res = await resolve_approval(
                store,
                approval.id,
                grant=True,
                actor=actor(),
                channel=channel,
            )
            if no_resume:
                return res, None
            outcome = await resume_run(
                res.run_id,
                project_root=root,
                registry=registry,
                store=store,
                config=resolved.config,
            )
            return res, outcome
        finally:
            await store.close()

    try:
        res, outcome = anyio.run(_go)
    except ConfigError as exc:
        fail(str(exc))
    except ToolFailure as exc:
        fail(str(exc), EXIT_ENVIRONMENT)
    typer.echo(f"Approval {res.approval.id} granted.")
    if outcome is None:
        typer.echo(f"Resume with: aix run --resume {res.run_id}")
        return
    print_outcome(outcome, "")
    raise typer.Exit(outcome.exit_code)


def deny(
    ref: Annotated[str, typer.Argument(help="Approval id or the task/run id it concerns.")],
    reason: Annotated[str | None, typer.Option("--reason", help="Why it is denied.")] = None,
    token: TokenOpt = None,
    project: ProjectOpt = Path(),
) -> None:
    """Deny a pending approval; the run fails."""
    from aix.core.orchestrator.approvals import find_approval, resolve_approval
    from aix.domain.errors import ConfigError
    from aix.security.approvals import actor

    root = project.resolve()
    load_or_exit(root)

    async def _find():  # type: ignore[no-untyped-def]
        store = await _open_store(root)
        try:
            return await find_approval(store, ref)
        finally:
            await store.close()

    try:
        approval, _run_id = anyio.run(_find)
    except ConfigError as exc:
        fail(str(exc))
    channel = _authenticate(approval.id, token)

    async def _go():  # type: ignore[no-untyped-def]
        store = await _open_store(root)
        try:
            return await resolve_approval(
                store,
                approval.id,
                grant=False,
                actor=actor(),
                channel=channel,
                reason=reason,
            )
        finally:
            await store.close()

    res = anyio.run(_go)
    typer.echo(f"Approval {res.approval.id} denied.")
    if res.run_failed:
        typer.echo(f"Run {res.run_id} FAILED: approval denied.")
        raise typer.Exit(1)
