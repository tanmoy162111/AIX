"""`aix agent ...` commands (PLAYBOOK §23.1)."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Annotated

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit, registry_or_exit

agent_app = typer.Typer(name="agent", help="Inspect and manage agents.", no_args_is_help=True)

ProjectOpt = Annotated[Path, typer.Option("--project", help="Project root.")]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


@agent_app.command("list")
def list_agents(as_json: JsonOpt = False, project: ProjectOpt = Path()) -> None:
    """List every known agent with its health (unavailable agents are listed too)."""
    resolved = load_or_exit(project)
    registry = registry_or_exit(resolved)
    specs = anyio.run(registry.probe_all)
    rows = [{**s.model_dump(mode="json"), "enabled": registry.is_enabled(s.id)} for s in specs]
    if as_json:
        typer.echo(json.dumps(rows, indent=2))
        return
    from rich.console import Console
    from rich.table import Table

    table = Table("id", "name", "health", "version", "cost", "enabled", "reason")
    for r in rows:
        table.add_row(
            r["id"], r["name"], r["health"], r["version"] or "-", r["cost_class"],
            "yes" if r["enabled"] else "no", r["health_reason"] or "",
        )  # fmt: skip
    Console(width=160).print(table)


@agent_app.command("inspect")
def inspect_agent(
    agent_id: Annotated[str, typer.Argument(help="Agent id.")],
    as_json: JsonOpt = False,
    project: ProjectOpt = Path(),
) -> None:
    """Show an agent's manifest, capabilities and current health."""
    from aix.domain.errors import ConfigError

    resolved = load_or_exit(project)
    registry = registry_or_exit(resolved)
    try:
        manifest = registry.manifest(agent_id)
    except ConfigError as exc:
        fail(str(exc))
    spec = anyio.run(registry.probe, agent_id)
    doc = {
        "manifest": manifest.model_dump(mode="json"),
        "spec": spec.model_dump(mode="json"),
        "enabled": registry.is_enabled(agent_id),
    }
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    import yaml

    typer.echo(yaml.safe_dump(doc, sort_keys=False))


@agent_app.command("test")
def test_agent(
    agent_id: Annotated[str, typer.Argument(help="Agent id.")],
    live: Annotated[
        bool, typer.Option("--live", help="Make a real (possibly paid) call to the agent.")
    ] = False,
    as_json: JsonOpt = False,
    project: ProjectOpt = Path(),
) -> None:
    """Probe an agent; with --live (or for `fake`) also run a tiny read-only smoke prompt."""
    from aix.agents.protocol import AgentPermissions, AgentRequest
    from aix.domain.errors import ConfigError
    from aix.domain.ids import IdPrefix, new_id

    resolved = load_or_exit(project)
    registry = registry_or_exit(resolved)
    try:
        registry.manifest(agent_id)
    except ConfigError as exc:
        fail(str(exc))
    spec = anyio.run(registry.probe, agent_id)
    doc: dict[str, object] = {
        "agent": agent_id,
        "health": spec.health,
        "health_reason": spec.health_reason,
        "smoke": None,
    }
    if not as_json:
        typer.echo(
            f"{agent_id}: health={spec.health}"
            + (f" ({spec.health_reason})" if spec.health_reason else "")
        )
    if spec.health in ("unavailable",):
        if as_json:
            typer.echo(json.dumps(doc))
        raise typer.Exit(EXIT_ENVIRONMENT)
    if not live and agent_id != "fake" and not agent_id.startswith("fake-"):
        if as_json:
            typer.echo(json.dumps(doc))
        else:
            typer.echo("probe only; pass --live to run a real smoke prompt (may cost money)")
        return

    adapter = registry.get(agent_id)

    async def smoke() -> tuple[str, str | None, str]:
        with tempfile.TemporaryDirectory(prefix="aix-agent-test-") as ws:
            req = AgentRequest(
                attempt_id=new_id(IdPrefix.ATTEMPT),
                workspace=Path(ws),
                prompt="Reply with the single word OK. Do not use any tools.",
                timeout_s=180,
                permissions=AgentPermissions(read_only=True),
            )
            h = await adapter.start(req)
            async for _ in adapter.events(h):
                pass
            out = await adapter.wait(h)
            return out.status, out.failure.value if out.failure else None, out.claim or ""

    status, failure, claim = anyio.run(smoke)
    if as_json:
        doc["smoke"] = {"status": status, "failure": failure, "claim_unverified": claim[:200]}
        typer.echo(json.dumps(doc))
        raise typer.Exit(0 if status == "completed" else 1)
    typer.echo(f"smoke: status={status}" + (f" failure={failure}" if failure else ""))
    if status != "completed":
        raise typer.Exit(1)
    typer.echo(f"claim (unverified): {claim[:200]}")


def _toggle(agent_id: str, enabled: bool, project: Path, as_json: bool = False) -> None:
    from aix.config.edit import set_agent_enabled
    from aix.domain.errors import ConfigError

    resolved = load_or_exit(project)
    registry = registry_or_exit(resolved)
    try:
        registry.manifest(agent_id)
    except ConfigError as exc:
        fail(str(exc))
    try:
        updated = set_agent_enabled(project.resolve(), agent_id, enabled)
    except ConfigError as exc:
        fail(str(exc))
    if as_json:
        typer.echo(json.dumps({"agent": agent_id, "enabled": enabled, "agents_enabled": updated}))
        return
    verb = "enabled" if enabled else "disabled"
    typer.echo(f"{agent_id} {verb}; agents.enabled = {', '.join(updated) or '(none)'}")


@agent_app.command("enable")
def enable(
    agent_id: Annotated[str, typer.Argument()],
    as_json: JsonOpt = False,
    project: ProjectOpt = Path(),
) -> None:
    """Add an agent to `agents.enabled` in .aix/config.yaml."""
    _toggle(agent_id, True, project, as_json)


@agent_app.command("disable")
def disable(
    agent_id: Annotated[str, typer.Argument()],
    as_json: JsonOpt = False,
    project: ProjectOpt = Path(),
) -> None:
    """Remove an agent from `agents.enabled` in .aix/config.yaml."""
    _toggle(agent_id, False, project, as_json)
