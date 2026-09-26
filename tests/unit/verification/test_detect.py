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


# ---- M3.1 repo facts inspector (§13.1) ----


def test_inspect_repo_python(tmp_path: Path) -> None:
    from aix.verification.detect import inspect_repo

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n[tool.pytest.ini_options]\n")
    (tmp_path / "uv.lock").write_text("")
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "b.py").write_text("y = 2\n")
    (tmp_path / "README.md").write_text("# hi\n")
    (tmp_path / "CLAUDE.md").write_text("rules\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("ignored")
    f = inspect_repo(tmp_path)
    assert f.languages == ["python"]
    assert f.package_managers == ["uv"]
    assert f.test_commands == ["pytest -q"]
    assert f.convention_files == ["CLAUDE.md", "README.md"]
    assert f.file_count == 6  # .git and node_modules are excluded
    assert f.is_git_repo is True


def test_inspect_repo_node_and_make(tmp_path: Path) -> None:
    from aix.verification.detect import inspect_repo

    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    (tmp_path / "pnpm-lock.yaml").write_text("")
    (tmp_path / "Makefile").write_text("test:\n\tgo test\n")
    (tmp_path / "x.ts").write_text("const a = 1\n")
    f = inspect_repo(tmp_path)
    assert f.languages == ["node"]
    assert f.package_managers == ["pnpm"]
    assert f.test_commands == ["make test", "npm test"]
    assert f.total_bytes > 0


def test_inspect_repo_missing_dir() -> None:
    from aix.verification.detect import inspect_repo

    f = inspect_repo(Path("/nonexistent-aix-dir"))
    assert f.languages == [] and f.file_count == 0 and f.is_git_repo is False
