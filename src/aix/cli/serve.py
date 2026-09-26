"""`aix serve`: the HTTP API on loopback (PLAYBOOK §24)."""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Annotated

import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(
    host: Annotated[str, typer.Option("--host", help="Interface to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535, help="Port to bind.")] = 8765,
    allow_remote: Annotated[
        bool,
        typer.Option("--allow-remote", help="Permit binding a non-loopback interface (unsafe)."),
    ] = False,
    show_token: Annotated[
        bool, typer.Option("--show-token", help="Print the API token (creates it) and exit.")
    ] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Serve the API: runs, events (SSE), approvals and artifacts, for this project."""
    from aix.security.approvals import ApprovalRefused, guard_agent_context

    if show_token:
        from aix.api.auth import ensure_api_token

        try:
            guard_agent_context()
        except ApprovalRefused as exc:
            fail(str(exc))
        typer.echo(ensure_api_token())
        return
    if not _is_loopback(host) and not allow_remote:
        fail(
            f"refusing to bind {host}: the API is plain HTTP with bearer tokens; "
            "pass --allow-remote only behind a trusted network or proxy"
        )
    root = project.resolve()
    load_or_exit(root)
    if not (root / ".aix" / "config.yaml").exists():
        fail("not initialized: run `aix init` first")
    try:
        import uvicorn

        from aix.api.app import create_app
    except ImportError:
        fail("the API needs its extra: pip install 'aix[api]'", EXIT_ENVIRONMENT)

    from aix.domain.errors import ConfigError

    try:
        app = create_app(root)
    except ConfigError as exc:
        fail(str(exc))
    typer.echo(f"aix API on http://{host}:{port}  (project {root})")
    typer.echo(
        "Authorize with `Authorization: Bearer <token>`: `aix serve --show-token` (run/read);"
    )
    typer.echo("approvals need the approval token: `aix approvals --show-token`.")
    uvicorn.run(app, host=host, port=port, log_level="warning")
