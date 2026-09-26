"""Async git helper with a deterministic, minimal environment.

Uses ``anyio.run_process`` (no streaming needed). The user's global/system git config is ignored so
hooks, signing and excludes cannot change aix's behavior; aix-authored commits use a fixed identity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import anyio

from aix.domain.errors import ToolFailure

AIX_IDENTITY_NAME = "aix"
AIX_IDENTITY_EMAIL = "aix@localhost"


@dataclass(frozen=True)
class GitResult:
    code: int
    stdout: str
    stderr: str


def git_env() -> dict[str, str]:
    """Environment for git subprocesses."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": AIX_IDENTITY_NAME,
        "GIT_AUTHOR_EMAIL": AIX_IDENTITY_EMAIL,
        "GIT_COMMITTER_NAME": AIX_IDENTITY_NAME,
        "GIT_COMMITTER_EMAIL": AIX_IDENTITY_EMAIL,
    }


async def git(repo: Path, *args: str, check: bool = True) -> GitResult:
    """Run ``git <args>`` in ``repo``.

    Raises:
        ToolFailure: git is missing, or ``check`` is set and git exited non-zero.
    """
    argv = ["git", *args]
    try:
        proc = await anyio.run_process(argv, cwd=repo, env=git_env(), check=False)
    except FileNotFoundError as exc:
        raise ToolFailure("git executable not found") from exc
    result = GitResult(
        proc.returncode,
        proc.stdout.decode("utf-8", errors="replace"),
        proc.stderr.decode("utf-8", errors="replace"),
    )
    if check and result.code != 0:
        raise ToolFailure(
            f"git {' '.join(args[:2])} failed: {result.stderr.strip() or result.stdout.strip()}",
            details={"argv": " ".join(argv), "code": result.code},
        )
    return result
