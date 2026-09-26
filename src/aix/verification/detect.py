"""Toolchain detection (PLAYBOOK §17.1).

This first slice only reports *which* ecosystems and entry points exist; command resolution
(build/test/lint/typecheck) is added in M4.1 and the repo-facts inspector in M3.1.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from aix.domain.base import DomainModel

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
