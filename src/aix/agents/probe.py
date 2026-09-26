"""Shared binary probing: presence, version, and required-flag discovery (PLAYBOOK §11).

"Discover, don't assume": the help text is searched for the manifest's ``required_flags``; missing
flags make the agent ``degraded`` with a reason. Probing never makes a paid call.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from aix.agents.manifest import AdapterManifest
from aix.agents.subprocess import spawn
from aix.domain.errors import ToolFailure

Health = Literal["ready", "degraded", "unavailable"]
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+.][0-9A-Za-z.]+)*")


@dataclass(frozen=True)
class BinaryProbe:
    health: Health
    version: str | None
    reason: str | None
    missing_flags: list[str] = field(default_factory=list[str])


async def _run(
    binary: str, args: list[str], env: dict[str, str], timeout_s: float
) -> tuple[int | None, str, bool]:
    """Run ``binary args``; returns (exit_code, stdout+stderr text, timed_out)."""
    out: list[str] = []
    with tempfile.TemporaryDirectory() as cwd:
        async with spawn(
            [binary, *args], cwd=Path(cwd), env=env, timeout_s=timeout_s, grace_s=1.0
        ) as proc:
            async for line in proc.lines():
                out.append(line)
            res = await proc.wait()
    return res.exit_code, "\n".join(out) + "\n" + res.stderr_tail, res.timed_out


async def probe_binary(
    manifest: AdapterManifest, *, env: dict[str, str] | None = None, timeout_s: float = 10.0
) -> BinaryProbe:
    """Probe the manifest's binary.

    Raises:
        ValueError: the manifest has no ``binary`` (in-process agents do not use this helper).
    """
    if manifest.binary is None:
        raise ValueError(f"manifest {manifest.id!r} has no binary to probe")
    env = dict(os.environ) if env is None else env
    search_path = env.get("PATH")
    resolved = shutil.which(manifest.binary, path=search_path)
    if resolved is None:
        return BinaryProbe("unavailable", None, f"binary {manifest.binary!r} not found on PATH")

    try:
        code, text, timed_out = await _run(resolved, manifest.probe.version_args, env, timeout_s)
    except ToolFailure as exc:
        return BinaryProbe("unavailable", None, str(exc))
    if timed_out:
        return BinaryProbe("unavailable", None, f"`{manifest.binary} --version` timed out")
    if code != 0:
        return BinaryProbe("unavailable", None, f"`{manifest.binary} --version` exited with {code}")
    match = _VERSION.search(text)
    version = match.group(0) if match else None

    missing: list[str] = []
    if manifest.required_flags:
        try:
            code, help_text, timed_out = await _run(
                resolved, manifest.probe.help_args, env, timeout_s
            )
        except ToolFailure as exc:
            return BinaryProbe("unavailable", version, str(exc))
        if timed_out:
            return BinaryProbe("unavailable", version, f"`{manifest.binary} --help` timed out")
        missing = [f for f in manifest.required_flags if f not in help_text]
    if missing:
        return BinaryProbe(
            "degraded", version, f"help output lacks required flags: {', '.join(missing)}", missing
        )
    return BinaryProbe("ready", version, None)
