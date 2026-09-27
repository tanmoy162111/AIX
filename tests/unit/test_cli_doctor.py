from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aix.cli.main import app
from cli_env import install

runner = CliRunner()


def doctor(env_dir: Path, *extra: str) -> tuple[int, dict[str, object]]:
    result = runner.invoke(app, ["doctor", "--json", "--project", str(env_dir / "proj"), *extra])
    return result.exit_code, json.loads(result.stdout)


def by_name(doc: dict[str, object]) -> dict[str, dict[str, str]]:
    return {c["name"]: c for c in doc["checks"]}  # type: ignore[index,union-attr]


def test_healthy_environment_exits_zero_with_warnings(env: Path) -> None:
    install(env, "claude")
    code, doc = doctor(env)
    checks = by_name(doc)
    assert code == 0 and doc["ok"] is True
    assert checks["python"]["status"] == "ok" and checks["git"]["status"] == "ok"
    assert checks["agent claude"]["status"] == "ok"
    assert checks["agent codex"]["status"] == "warn"
    assert checks["jev key"]["status"] == "warn"
    assert checks["tool semgrep"]["status"] == "warn"
    assert checks["container runtime"]["status"] == "warn"


def test_jev_key_is_detected(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TYPESAFE_API_KEY", "x")
    _, doc = doctor(env)
    assert by_name(doc)["jev key"]["status"] == "ok"


def test_missing_git_is_an_environment_failure(env: Path) -> None:
    (env / "bin" / "git").unlink()
    code, doc = doctor(env)
    assert code == 5 and doc["ok"] is False
    assert by_name(doc)["git"]["status"] == "fail"


def test_invalid_config_is_reported_as_a_failure(env: Path) -> None:
    (env / "proj" / ".aix" / "config.yaml").write_text("bogus_key: 1\n")
    code, doc = doctor(env)
    assert code == 5 and by_name(doc)["config"]["status"] == "fail"


def test_no_ready_agent_and_missing_init_are_warnings(env: Path) -> None:
    (env / "proj" / ".aix" / "config.yaml").unlink(missing_ok=True)
    code, doc = doctor(env)
    checks = by_name(doc)
    assert code == 0
    assert checks["agents"]["status"] == "warn"
    assert checks["project"]["status"] == "warn"


def test_table_output(env: Path) -> None:
    result = runner.invoke(app, ["doctor", "--project", str(env / "proj")])
    assert result.exit_code == 0 and "python" in result.stdout and "git" in result.stdout


def test_a_healthy_environment_lists_no_plugins(env: Path) -> None:
    _, doc = doctor(env)
    assert not [n for n in by_name(doc) if n.startswith("plugin ")]


def test_installed_plugins_are_listed_and_broken_ones_are_warnings(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugin_env import install_sample_plugin

    install_sample_plugin(tmp_path / "site", monkeypatch)
    code, doc = doctor(env)
    checks = by_name(doc)
    assert code == 0 and doc["ok"] is True  # a broken plugin never fails the CLI
    good = checks["plugin adapter sample-agent"]
    assert good["status"] == "ok" and "0.1.0" in good["detail"]
    assert "spawn_process" in good["detail"]
    assert checks["plugin check no-todo"]["status"] == "ok"
    future = checks["plugin adapter future-agent"]
    assert future["status"] == "warn" and "requires aix >=99.0" in future["detail"]
    ghost = checks["plugin adapter ghost-agent"]
    assert ghost["status"] == "warn" and "does_not_exist" in ghost["detail"]


def test_a_check_plugin_that_cannot_be_imported_is_reported(
    env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from plugin_env import install_sample_plugin

    site = install_sample_plugin(tmp_path / "site", monkeypatch)
    (site / "aix_sample_plugin" / "checks.py").write_text(
        "raise RuntimeError('broken on import')\n"
    )
    _, doc = doctor(env)
    check = by_name(doc)["plugin check no-todo"]
    assert check["status"] == "warn" and "broken on import" in check["detail"]
