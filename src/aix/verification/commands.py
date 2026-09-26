"""Resolve build/test/lint/typecheck commands for a project (PLAYBOOK §17.1)."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Literal

from aix.domain.base import DomainModel
from aix.domain.enums import CheckKind as K
from aix.verification.detect import configures_pytest, detect_toolchain

Which = Callable[[str], str | None]


class ResolvedCommand(DomainModel):
    """A command for one check kind, with where it came from (recorded in the report)."""

    argv: list[str]
    source: Literal["config", "detected"]
    available: bool
    """Executable is on PATH; a required check with a missing tool ends ``skipped`` (§17.2)."""


def _pyproject_text(root: Path) -> str:
    try:
        return (root / "pyproject.toml").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _python(root: Path, which: Which) -> dict[K, list[str]]:
    text = _pyproject_text(root)
    out: dict[K, list[str]] = {
        K.BUILD: ["python", "-m", "compileall", "-q", "src" if (root / "src").is_dir() else "."]
    }
    if configures_pytest(root):
        out[K.TESTS] = ["pytest", "-q"]
    if which("ruff"):
        out[K.LINT] = ["ruff", "check"]
    if "[tool.pyright]" in text or (root / "pyrightconfig.json").is_file():
        out[K.TYPECHECK] = ["pyright"]
    elif "[tool.mypy]" in text or (root / "mypy.ini").is_file():
        out[K.TYPECHECK] = ["mypy", "."]
    return out


def _node(root: Path, scripts: list[str]) -> dict[K, list[str]]:
    out: dict[K, list[str]] = {}
    if "build" in scripts:
        out[K.BUILD] = ["npm", "run", "build"]
    if "test" in scripts:
        out[K.TESTS] = ["npm", "test"]
    if "lint" in scripts:
        out[K.LINT] = ["npm", "run", "lint"]
    if "typecheck" in scripts:
        out[K.TYPECHECK] = ["npm", "run", "typecheck"]
    elif (root / "tsconfig.json").is_file():
        out[K.TYPECHECK] = ["npx", "tsc", "--noEmit"]
    return out


def resolve_commands(
    root: Path, config: Mapping[K, list[str]], *, which: Which = shutil.which
) -> dict[K, ResolvedCommand]:
    """Commands per check kind. Explicit ``config`` entries always win over detection.

    Detection order per kind: python, node, go, rust ecosystems, then ``Makefile`` targets as a
    fallback for kinds nothing else supplied. Kinds with no command are absent from the result.
    """
    tc = detect_toolchain(root)
    found: dict[K, list[str]] = {}

    def add(cmds: dict[K, list[str]]) -> None:
        for kind, argv in cmds.items():
            found.setdefault(kind, argv)

    for eco in tc.ecosystems:
        if eco == "python":
            add(_python(root, which))
        elif eco == "node":
            add(_node(root, tc.package_scripts))
        elif eco == "go":
            add({K.BUILD: ["go", "build", "./..."], K.TESTS: ["go", "test", "./..."],
                 K.LINT: ["go", "vet", "./..."]})  # fmt: skip
        elif eco == "rust":
            add({K.BUILD: ["cargo", "build"], K.TESTS: ["cargo", "test"],
                 K.LINT: ["cargo", "clippy"]})  # fmt: skip
    for kind, target in ((K.BUILD, "build"), (K.TESTS, "test"), (K.LINT, "lint")):
        if target in tc.make_targets:
            found.setdefault(kind, ["make", target])

    result = {
        kind: ResolvedCommand(argv=argv, source="detected", available=bool(which(argv[0])))
        for kind, argv in found.items()
    }
    for kind, argv in config.items():
        if argv:
            result[kind] = ResolvedCommand(
                argv=list(argv), source="config", available=bool(which(argv[0]))
            )
    return result
