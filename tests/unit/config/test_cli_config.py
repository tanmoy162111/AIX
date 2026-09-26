from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from aix.cli.main import app

runner = CliRunner()


def _project(tmp_path: Path, text: str = "") -> Path:
    (tmp_path / ".aix").mkdir()
    (tmp_path / ".aix" / "config.yaml").write_text(text)
    return tmp_path


def test_show_prints_effective_config_as_yaml(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = _project(tmp_path, "execution: {max_parallel: 2}\n")
    result = runner.invoke(app, ["config", "show", "--project", str(proj)])
    assert result.exit_code == 0, result.output
    doc = yaml.safe_load(result.stdout)
    assert doc["execution"]["max_parallel"] == 2
    assert doc["planner"]["max_tasks"] == 12


def test_show_resolved_prints_sources(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = _project(tmp_path, "execution: {max_parallel: 2}\n")
    result = runner.invoke(app, ["config", "show", "--resolved", "--project", str(proj), "--json"])
    assert result.exit_code == 0, result.output
    rows = {r["key"]: r for r in json.loads(result.stdout)}
    assert rows["execution.max_parallel"]["value"] == 2
    assert rows["execution.max_parallel"]["source"].startswith("project:")
    assert rows["routing.strategy"]["source"] == "default"


def test_show_resolved_table_mentions_key_and_source(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = _project(tmp_path)
    result = runner.invoke(app, ["config", "show", "--resolved", "--project", str(proj)])
    assert result.exit_code == 0
    assert "planner.max_tasks" in result.stdout and "default" in result.stdout


def test_invalid_config_exits_2(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = _project(tmp_path, "execution: {max_parallel: 0}\n")
    result = runner.invoke(app, ["config", "show", "--project", str(proj)])
    assert result.exit_code == 2
    assert "execution.max_parallel" in result.output
