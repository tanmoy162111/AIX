"""`aix run` through the real CLI entry point with the scripted fake agent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repos
from aix.cli.main import app

runner = CliRunner()
SCRIPTS = repos.FIXTURES / "agent_scripts"


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    return proj


def aix_run(repo: Path, script: str | None, *extra: str, agent: str = "fake") -> tuple[int, str]:
    import os

    if script:
        os.environ["AIX_FAKE_SCRIPTS"] = str(SCRIPTS / script)
    else:
        os.environ.pop("AIX_FAKE_SCRIPTS", None)
    try:
        r = runner.invoke(
            app, ["run", "Add a hello endpoint", "--agent", agent, "--project", str(repo), *extra]
        )
    finally:
        os.environ.pop("AIX_FAKE_SCRIPTS", None)
    return r.exit_code, r.output


def test_successful_run_prints_summary_and_lands_on_the_run_branch(repo: Path) -> None:
    code, out = aix_run(repo, "hello.yaml")
    assert code == 0, out
    assert "COMPLETED" in out and "branch: aix/run/run_" in out
    assert "Changes      2 files" in out and "Agent claim (unverified)" in out
    assert "Next: git merge aix/run/" in out
    branch = next(w for w in out.split() if w.startswith("aix/run/"))
    assert "/hello" in repos.git(repo, "show", f"{branch}:app/handler.py").stdout
    assert "hello" not in (repo / "app" / "handler.py").read_text()  # user's tree untouched


def test_json_output(repo: Path) -> None:
    code, out = aix_run(repo, "hello.yaml", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["status"] == "completed" and doc["exit_code"] == 0
    assert doc["diff"]["paths"] == ["app/handler.py", "tests/test_hello.py"]
    assert doc["usage"]["cost_usd"] == 0.02 and doc["failure"] is None


def test_failed_run_exits_1(repo: Path) -> None:
    code, out = aix_run(repo, "noop_lying.yaml")
    assert code == 1 and "FAILED" in out and "agent_no_changes" in out
    assert "Next: git merge" not in out


def test_scope_option_is_enforced(repo: Path) -> None:
    code, out = aix_run(repo, "outside_scope.yaml", "--scope", "app/**", "--scope", "tests/**")
    assert code == 1 and "scope_violation" in out


def test_keep_worktrees_flag(repo: Path) -> None:
    _, out = aix_run(repo, "hello.yaml", "--keep-worktrees", "--json")
    att = json.loads(out)["attempt_id"]
    assert (repo / ".aix" / "worktrees" / att).is_dir()


def test_not_initialized_is_a_usage_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    proj = repos.materialize_sample_py(tmp_path / "p")
    code, out = aix_run(proj, "hello.yaml")
    assert code == 2 and "aix init" in out


def test_unknown_agent_is_a_usage_error(repo: Path) -> None:
    code, out = aix_run(repo, None, agent="nope")
    assert code == 2 and "unknown agent" in out


def test_unavailable_agent_is_an_environment_error(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    code, out = aix_run(repo, None, agent="claude")
    assert code == 5 and "unavailable" in out


def test_dirty_tree_is_a_usage_error_and_allow_dirty_works(repo: Path) -> None:
    (repo / "README.md").write_text("dirty\n")
    code, out = aix_run(repo, "hello.yaml")
    assert code == 2 and "uncommitted" in out
    code, out = aix_run(repo, "hello.yaml", "--allow-dirty")
    assert code == 0, out


def test_not_a_git_repo_is_an_environment_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    plain = tmp_path / "plain"
    plain.mkdir()
    assert runner.invoke(app, ["init", "--project", str(plain)]).exit_code == 0
    code, out = aix_run(plain, "hello.yaml")
    assert code == 5 and "not a git repository" in out


def test_events_are_queryable_after_the_run(repo: Path) -> None:
    import sqlite3

    aix_run(repo, "hello.yaml")
    con = sqlite3.connect(repo / ".aix" / "aix.db")
    types = [r[0] for r in con.execute("SELECT type FROM events ORDER BY seq")]
    assert types[0] == "run.created" and types[-1] == "run.completed"
    assert con.execute("SELECT status FROM runs").fetchone()[0] == "completed"
