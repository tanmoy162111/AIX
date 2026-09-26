"""``policy`` check: scope, forbidden file types, large binary blobs (PLAYBOOK §17.3)."""

from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath
from typing import Final

import anyio

from aix.core.workspace.scope import scope_violations
from aix.domain.enums import CheckKind
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check

MAX_BINARY_BYTES: Final = 1024 * 1024
_FORBIDDEN_NAMES: Final = ("id_rsa", "id_ed25519", "credentials.json")
_FORBIDDEN_GLOBS: Final = ("*.pem", "*.key", "*.p12", "*.pfx", "*.keystore")
_ENV_SAFE_SUFFIXES: Final = (".example", ".sample", ".template", ".dist")


def _forbidden(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    if name == ".env" or (name.startswith(".env.") and not name.endswith(_ENV_SAFE_SUFFIXES)):
        return True
    if name in _FORBIDDEN_NAMES or any(fnmatch.fnmatch(name, g) for g in _FORBIDDEN_GLOBS):
        return True
    return ".aws" in p.parts[:-1] and name == "credentials"


def _is_large_binary(root: Path, rel: str, limit: int) -> bool:
    path = root / rel
    try:
        if not path.is_file() or path.stat().st_size <= limit:
            return False
        with path.open("rb") as fh:
            return b"\x00" in fh.read(8192)
    except OSError:
        return False


def _collect(root: Path, paths: list[str], scope: list[str], limit: int) -> list[str]:
    problems: list[str] = []
    for path in scope_violations(paths, scope, read_only=not scope):
        why = "changed in a read-only task" if not scope else "outside the task's file scope"
        problems.append(f"{path}: {why}")
    for path in paths:
        if _forbidden(path):
            problems.append(f"{path}: forbidden file type")
        elif _is_large_binary(root, path, limit):
            problems.append(f"{path}: binary blob larger than {limit} bytes")
    return problems


async def run_policy_check(
    workspace: Path,
    changed_paths: list[str],
    file_scope: list[str],
    *,
    max_binary_bytes: int = MAX_BINARY_BYTES,
) -> Check:
    """Check an attempt's changed paths against scope and file policy. Always ``required``.

    ``file_scope`` empty means read-only. Any violation is ``failed`` (severity ``high``);
    ``.aix/`` and ``.git/`` are always off limits. Files that no longer exist are not size checked.
    """
    problems = await anyio.to_thread.run_sync(
        _collect, workspace, changed_paths, file_scope, max_binary_bytes
    )
    if not problems:
        return Check(
            id=new_id(IdPrefix.CHECK),
            kind=CheckKind.POLICY,
            status="passed",
            required=True,
            summary="no policy violations",
            metrics={"policy_violations": 0.0},
        )
    shown = "; ".join(problems[:5]) + (f"; +{len(problems) - 5} more" if len(problems) > 5 else "")
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=CheckKind.POLICY,
        status="failed",
        severity="high",
        required=True,
        summary=f"{len(problems)} policy violation(s): {shown}",
        metrics={"policy_violations": float(len(problems))},
    )
