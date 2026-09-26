"""Materialize the fixture repository into a temp dir as a real git repo (PLAYBOOK §27.2)."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SAMPLE_PY = FIXTURES / "repos" / "sample_py"
PATCHES = FIXTURES / "agent_scripts" / "patches"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "aix-test",
    "GIT_AUTHOR_EMAIL": "aix-test@example.invalid",
    "GIT_COMMITTER_NAME": "aix-test",
    "GIT_COMMITTER_EMAIL": "aix-test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    import os

    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=check,
    )


def materialize_sample_py(dest: Path) -> Path:
    """Copy ``sample_py`` to ``dest``, ``git init`` it on ``main`` and commit; returns ``dest``."""
    shutil.copytree(
        SAMPLE_PY,
        dest,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", ".ruff_cache"),
    )
    git(dest, "init", "-q", "-b", "main")
    git(dest, "add", "-A")
    git(dest, "commit", "-q", "-m", "baseline")
    return dest


def apply_patch(repo: Path, name: str) -> None:
    """``git apply`` the named fixture patch (e.g. ``hello.diff``) into the working tree."""
    git(repo, "apply", "--whitespace=nowarn", str(PATCHES / name))


def run_pytest(repo: Path) -> subprocess.CompletedProcess[str]:
    """Run the fixture's own test suite with the current interpreter."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
