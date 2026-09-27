"""`aix doctor`: environment checks (PLAYBOOK §23.1)."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import anyio
import typer

from aix.cli.common import EXIT_ENVIRONMENT

if TYPE_CHECKING:
    from aix.plugins.loader import PluginRecord

Status = Literal["ok", "warn", "fail"]
OPTIONAL_TOOLS = ("semgrep", "gitleaks", "pip-audit", "bandit")


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def _version(binary: str) -> str:
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
        return (
            (out.stdout or out.stderr).strip().splitlines()[0] if (out.stdout or out.stderr) else ""
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _plugin_checks(adapter_records: list[PluginRecord]) -> list[Check]:
    """External plugins of every type: a failing one is a warning, never a doctor failure.

    Adapter records come from the registry (already loaded); other types are discovered here.
    Valid manifests are activated once (imported) so a plugin broken at import time shows up.
    """
    from aix.plugins.loader import BUILTIN, discover, load_object
    from aix.plugins.manifest import PluginType

    records = [r for r in adapter_records if r.source != BUILTIN]
    for kind in PluginType:
        if kind is not PluginType.ADAPTER:
            records.extend(r for r in discover(kind) if r.source != BUILTIN)
    out: list[Check] = []
    for r in records:
        name = f"plugin {r.type.value} {r.id}"
        error = r.error
        if error is None and r.type is not PluginType.ADAPTER:
            try:
                load_object(r)
            except Exception as exc:  # third-party code is imported here
                error = str(exc)
        if error is not None:
            out.append(Check(name, "warn", f"skipped: {error} [{r.source}]"))
            continue
        assert r.manifest is not None
        perms = (
            f"; permissions: {', '.join(r.manifest.permissions)}" if r.manifest.permissions else ""
        )
        out.append(Check(name, "ok", f"v{r.manifest.version} from {r.source}{perms}"))
    return out


def collect_checks(project: Path) -> list[Check]:
    """Run every doctor check. Only python, git and config problems are failures."""
    from aix.agents.registry import AdapterRegistry
    from aix.config.loader import load_config
    from aix.domain.errors import ConfigError

    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 12)
    checks.append(
        Check("python", "ok" if py_ok else "fail", f"{platform.python_version()} (need >= 3.12)")
    )

    git = shutil.which("git")
    checks.append(
        Check("git", "ok", _version("git"))
        if git
        else Check("git", "fail", "git not found on PATH")
    )

    try:
        resolved = load_config(project)
    except ConfigError as exc:
        checks.append(Check("config", "fail", str(exc)))
        resolved = None
    else:
        files = ", ".join(str(p) for p in resolved.files) or "defaults only"
        checks.append(Check("config", "ok", files))

    if (project / ".aix" / "config.yaml").exists():
        checks.append(Check("project", "ok", f"initialized ({project / '.aix'})"))
    else:
        checks.append(Check("project", "warn", "not initialized: run `aix init`"))
    if git and not (project / ".git").exists():
        checks.append(Check("git repo", "warn", "not a git repository; `aix run` needs one"))

    if resolved is not None:
        registry = AdapterRegistry(resolved.config)
        specs = anyio.run(registry.probe_all)
        real_ready = [s for s in specs if s.health in ("ready", "degraded") and s.kind != "local"]
        for s in specs:
            if s.id.startswith("fake"):
                continue
            status: Status = "ok" if s.health == "ready" else "warn"
            detail = f"{s.health}" + (f" v{s.version}" if s.version else "")
            if s.health_reason:
                detail += f": {s.health_reason}"
            checks.append(Check(f"agent {s.id}", status, detail))
        if not real_ready:
            checks.append(Check("agents", "warn", "no real agent is ready or enabled"))
        checks.extend(_plugin_checks(registry.plugin_records()))
    else:
        checks.extend(_plugin_checks([]))

    runtime = next((r for r in ("docker", "podman") if shutil.which(r)), None)
    checks.append(
        Check("container runtime", "ok", runtime)
        if runtime
        else Check("container runtime", "warn", "none (container sandbox unavailable)")
    )
    checks.append(
        Check("jev key", "ok", "TYPESAFE_API_KEY is set")
        if os.environ.get("TYPESAFE_API_KEY")
        else Check("jev key", "warn", "TYPESAFE_API_KEY not set (rules provider will be used)")
    )
    for tool in OPTIONAL_TOOLS:
        checks.append(
            Check(f"tool {tool}", "ok", shutil.which(tool) or "")
            if shutil.which(tool)
            else Check(f"tool {tool}", "warn", "not installed (related checks will be skipped)")
        )
    return checks


def doctor(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
    project: Annotated[Path, typer.Option("--project", help="Project root.")] = Path(),
) -> None:
    """Check git, python, agents, container runtime, Jev key and security tools."""
    checks = collect_checks(project.resolve())
    failed = any(c.status == "fail" for c in checks)
    if as_json:
        typer.echo(json.dumps({"ok": not failed, "checks": [asdict(c) for c in checks]}, indent=2))
    else:
        from rich.console import Console
        from rich.table import Table

        table = Table("check", "status", "detail")
        style = {"ok": "green", "warn": "yellow", "fail": "red"}
        for c in checks:
            table.add_row(c.name, f"[{style[c.status]}]{c.status}[/]", c.detail)
        Console(width=160).print(table)
    if failed:
        raise typer.Exit(EXIT_ENVIRONMENT)
