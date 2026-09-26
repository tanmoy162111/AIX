from __future__ import annotations

import json
from pathlib import Path

from aix.verification.detect import detect_toolchain


def test_empty_directory() -> None:
    s = detect_toolchain(Path("/nonexistent-aix-dir"))
    assert s.ecosystems == [] and s.markers == [] and s.is_git_repo is False


def test_python_project_with_makefile(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "Makefile").write_text(
        ".PHONY: test lint\n\ntest:\n\tpytest\nlint:\n\truff check .\n\nVAR := 1\n"
    )
    (tmp_path / ".git").mkdir()
    s = detect_toolchain(tmp_path)
    assert s.ecosystems == ["python"]
    assert s.markers == ["Makefile", "pyproject.toml"]
    assert s.make_targets == ["lint", "test"]
    assert s.is_git_repo is True


def test_node_scripts_are_listed(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "tsc", "test": "jest", "lint": "eslint ."}})
    )
    s = detect_toolchain(tmp_path)
    assert s.ecosystems == ["node"]
    assert s.package_scripts == ["build", "lint", "test"]


def test_malformed_package_json_does_not_crash(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{not json")
    assert detect_toolchain(tmp_path).ecosystems == ["node"]


def test_go_and_rust(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "Cargo.toml").write_text("[package]\n")
    assert detect_toolchain(tmp_path).ecosystems == ["go", "rust"]
