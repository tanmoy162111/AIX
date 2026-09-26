from __future__ import annotations

import json
from pathlib import Path

from aix.domain.enums import CheckKind as K
from aix.verification.commands import resolve_commands

ALL = {"python", "pytest", "ruff", "mypy", "pyright", "npm", "npx", "go", "cargo", "make", "tsc"}


def resolve(root: Path, config: dict[K, list[str]] | None = None, have: set[str] = ALL):  # type: ignore[no-untyped-def]
    return resolve_commands(root, config or {}, which=lambda b: f"/bin/{b}" if b in have else None)


def test_python_project(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n[tool.ruff]\n[tool.pyright]\n"
    )
    r = resolve(tmp_path)
    assert r[K.BUILD].argv == ["python", "-m", "compileall", "-q", "src"]
    assert r[K.TESTS].argv == ["pytest", "-q"]
    assert r[K.LINT].argv == ["ruff", "check"]
    assert r[K.TYPECHECK].argv == ["pyright"]
    assert all(c.source == "detected" and c.available for c in r.values())


def test_python_without_configured_tools(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    r = resolve(tmp_path)
    assert K.TESTS not in r and K.TYPECHECK not in r
    assert r[K.BUILD].argv == ["python", "-m", "compileall", "-q", "."]
    assert r[K.LINT].argv == ["ruff", "check"]


def test_missing_binary_is_reported_unavailable(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n")
    r = resolve(tmp_path, have={"python"})
    assert r[K.TESTS].available is False and r[K.BUILD].available is True
    assert K.LINT not in r  # ruff is only proposed when installed


def test_node_scripts_and_tsc(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "x", "test": "y", "lint": "z"}})
    )
    (tmp_path / "tsconfig.json").write_text("{}")
    r = resolve(tmp_path)
    assert r[K.BUILD].argv == ["npm", "run", "build"]
    assert r[K.TESTS].argv == ["npm", "test"]
    assert r[K.LINT].argv == ["npm", "run", "lint"]
    assert r[K.TYPECHECK].argv == ["npx", "tsc", "--noEmit"]


def test_node_typecheck_script_beats_tsc(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"typecheck": "t"}}))
    (tmp_path / "tsconfig.json").write_text("{}")
    assert resolve(tmp_path)[K.TYPECHECK].argv == ["npm", "run", "typecheck"]


def test_go_rust(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    r = resolve(tmp_path)
    assert r[K.BUILD].argv == ["go", "build", "./..."]
    assert r[K.TESTS].argv == ["go", "test", "./..."] and r[K.LINT].argv == ["go", "vet", "./..."]
    (tmp_path / "go.mod").unlink()
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    r = resolve(tmp_path)
    assert r[K.LINT].argv == ["cargo", "clippy"] and r[K.TESTS].argv == ["cargo", "test"]


def test_makefile_fills_gaps_only(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("test:\n\techo\nlint:\n\techo\n")
    r = resolve(tmp_path)
    assert r[K.TESTS].argv == ["make", "test"] and r[K.LINT].argv == ["make", "lint"]
    assert K.BUILD not in r
    (tmp_path / "go.mod").write_text("module x\n")
    assert resolve(tmp_path)[K.TESTS].argv == ["go", "test", "./..."]


def test_explicit_config_always_wins(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    r = resolve(tmp_path, {K.TESTS: ["just", "test"]})
    assert r[K.TESTS].argv == ["just", "test"] and r[K.TESTS].source == "config"
    assert r[K.TESTS].available is False  # `just` is not on this fake PATH
    assert r[K.BUILD].source == "detected"


def test_empty_project_detects_nothing(tmp_path: Path) -> None:
    assert resolve(tmp_path) == {}
