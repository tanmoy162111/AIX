"""Toolchain detection (PLAYBOOK §17.1) and the repo-facts inspector (§13.1).

This first slice only reports *which* ecosystems and entry points exist; command resolution
(build/test/lint/typecheck) is added in M4.1 and the repo-facts inspector in M3.1.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Literal

from aix.domain.base import DomainModel
from aix.domain.tasks import RepoFacts

Ecosystem = Literal["python", "node", "go", "rust"]

_MARKERS: tuple[tuple[str, Ecosystem | None], ...] = (
    ("pyproject.toml", "python"),
    ("setup.cfg", "python"),
    ("setup.py", "python"),
    ("package.json", "node"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
    ("Makefile", None),
)
_ECOSYSTEM_ORDER: tuple[Ecosystem, ...] = ("python", "node", "go", "rust")
_MAKE_TARGET = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.-]*)\s*:(?![=])")


class ToolchainSummary(DomainModel):
    """What was found in a project directory."""

    ecosystems: list[Ecosystem]
    markers: list[str]
    make_targets: list[str]
    package_scripts: list[str]
    is_git_repo: bool


def _make_targets(makefile: Path) -> list[str]:
    try:
        lines = makefile.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return sorted({m.group(1) for line in lines if (m := _MAKE_TARGET.match(line))})


def _package_scripts(package_json: Path) -> list[str]:
    try:
        doc = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    scripts = doc.get("scripts") if isinstance(doc, dict) else None
    return sorted(str(k) for k in scripts) if isinstance(scripts, dict) else []  # pyright: ignore[reportUnknownVariableType]


def detect_toolchain(root: Path) -> ToolchainSummary:
    """Inspect ``root`` for known build-tool marker files. Never raises for a missing directory."""
    present = [name for name, _ in _MARKERS if (root / name).is_file()]
    found = {eco for name, eco in _MARKERS if name in present and eco is not None}
    return ToolchainSummary(
        ecosystems=[e for e in _ECOSYSTEM_ORDER if e in found],
        markers=sorted(present),
        make_targets=_make_targets(root / "Makefile") if "Makefile" in present else [],
        package_scripts=_package_scripts(root / "package.json")
        if "package.json" in present
        else [],
        is_git_repo=(root / ".git").exists(),
    )


_LOCKFILES: tuple[tuple[str, str], ...] = (
    ("uv.lock", "uv"),
    ("poetry.lock", "poetry"),
    ("Pipfile.lock", "pipenv"),
    ("requirements.txt", "pip"),
    ("pnpm-lock.yaml", "pnpm"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm"),
    ("go.mod", "go"),
    ("Cargo.lock", "cargo"),
)
_CONVENTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "README.md")
_SKIP_DIRS = frozenset({".git", ".aix", "node_modules", ".venv", "venv", "__pycache__", "target"})


def _configures_pytest(root: Path) -> bool:
    for name in ("pytest.ini", "tox.ini"):
        if (root / name).is_file():
            return True
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return "pytest" in text


def _size(root: Path) -> tuple[int, int]:
    count = total = 0
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name not in _SKIP_DIRS:
                    stack.append(entry)
            elif entry.is_file():
                count += 1
                with contextlib.suppress(OSError):
                    total += entry.stat().st_size
    return count, total


def inspect_repo(root: Path) -> RepoFacts:
    """Cheap local inspection of ``root`` (§13.1): languages, managers, test commands, size.

    Reads only marker files and directory metadata; never raises for a missing directory.
    """
    tc = detect_toolchain(root)
    managers = [tool for name, tool in _LOCKFILES if (root / name).is_file()]
    if "node" in tc.ecosystems and not any(m in managers for m in ("pnpm", "yarn", "npm")):
        managers.append("npm")
    tests: list[str] = []
    if "test" in tc.make_targets:
        tests.append("make test")
    if "test" in tc.package_scripts:
        tests.append("npm test")
    if "python" in tc.ecosystems and _configures_pytest(root):
        tests.append("pytest -q")
    if "go" in tc.ecosystems:
        tests.append("go test ./...")
    if "rust" in tc.ecosystems:
        tests.append("cargo test")
    count, total = _size(root) if root.is_dir() else (0, 0)
    return RepoFacts(
        languages=list(tc.ecosystems),
        package_managers=managers,
        test_commands=tests,
        convention_files=[n for n in _CONVENTION_FILES if (root / n).is_file()],
        file_count=count,
        total_bytes=total,
        is_git_repo=tc.is_git_repo,
    )
