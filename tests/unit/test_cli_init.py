from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from aix.cli.main import app
from aix.config.loader import load_config
from aix.config.schema import AixConfig
from aix.store import migrations

runner = CliRunner()


def _init(project: Path, *extra: str) -> tuple[int, str]:
    result = runner.invoke(app, ["init", "--project", str(project), *extra])
    return result.exit_code, result.output


def test_init_creates_layout_config_and_database(tmp_path: Path) -> None:
    code, out = _init(tmp_path)
    assert code == 0, out
    assert (tmp_path / ".aix" / "config.yaml").is_file()
    db = tmp_path / ".aix" / "aix.db"
    assert db.is_file()
    version = sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0]
    assert version == migrations.latest_version()


def test_generated_config_is_valid_and_changes_nothing(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    _init(tmp_path)
    resolved = load_config(tmp_path)
    assert resolved.config == AixConfig()
    assert set(resolved.sources.values()) == {"default"}
    text = (tmp_path / ".aix" / "config.yaml").read_text()
    assert "max_parallel" in text  # documents the defaults, commented out


def test_gitignore_entries_are_added_once(tmp_path: Path) -> None:
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    _init(tmp_path)
    _init(tmp_path)
    lines = (tmp_path / ".gitignore").read_text().splitlines()
    assert lines[0] == "node_modules/"
    for entry in (".aix/worktrees/", ".aix/runs/"):
        assert lines.count(entry) == 1


def test_gitignore_is_created_and_file_without_trailing_newline_is_handled(tmp_path: Path) -> None:
    _init(tmp_path)
    assert ".aix/worktrees/" in (tmp_path / ".gitignore").read_text()
    other = tmp_path / "b"
    other.mkdir()
    (other / ".gitignore").write_text("dist")
    _init(other)
    assert (other / ".gitignore").read_text().splitlines()[:2] == ["dist", "# aix runtime files"]


def test_init_is_idempotent_and_keeps_edits(tmp_path: Path) -> None:
    _init(tmp_path)
    cfg = tmp_path / ".aix" / "config.yaml"
    cfg.write_text("execution: {max_parallel: 1}\n")
    code, out = _init(tmp_path)
    assert code == 0
    assert "already initialized" in out
    assert cfg.read_text() == "execution: {max_parallel: 1}\n"


def test_force_rewrites_config_but_keeps_database(tmp_path: Path) -> None:
    _init(tmp_path)
    cfg = tmp_path / ".aix" / "config.yaml"
    cfg.write_text("execution: {max_parallel: 1}\n")
    db = tmp_path / ".aix" / "aix.db"
    marker = db.stat().st_ino
    code, _ = _init(tmp_path, "--force")
    assert code == 0
    assert "max_parallel: 1}" not in cfg.read_text()
    assert db.stat().st_ino == marker


def test_toolchain_summary_is_reported(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / ".git").mkdir()
    code, out = _init(tmp_path, "--json")
    assert code == 0
    doc = json.loads(out)
    assert doc["toolchain"]["ecosystems"] == ["python"]
    assert doc["toolchain"]["is_git_repo"] is True
    assert doc["config"].endswith("config.yaml")


def test_warns_when_not_a_git_repo(tmp_path: Path) -> None:
    code, out = _init(tmp_path)
    assert code == 0
    assert "not a git repository" in out
