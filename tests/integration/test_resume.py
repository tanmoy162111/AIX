"""Crash recovery: kill the orchestrator mid-attempt, then `aix run --resume` (M7.8)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repos
from aix.cli.main import app
from cli_env import hermetic
from resume_env import db_scalar, kill_mid_attempt, write_scripts
from verif_env import write_fast_config

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic(tmp_path, monkeypatch)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    write_fast_config(proj)
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(write_scripts(tmp_path / "scripts")))
    return proj


def test_kill_then_resume_completes_the_run(repo: Path, tmp_path: Path) -> None:
    run_id = kill_mid_attempt(repo, tmp_path)
    assert db_scalar(repo, "SELECT status FROM runs") == "executing"
    assert (repo / ".aix" / "runs" / run_id / "orchestrator.pid").exists()  # left by the crash

    res = runner.invoke(app, ["run", "--resume", run_id, "--json", "--project", str(repo)])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["status"] == "completed" and {t["status"] for t in doc["tasks"]} == {"completed"}

    con = sqlite3.connect(repo / ".aix" / "aix.db")
    try:
        interrupted = con.execute(
            "SELECT count(*) FROM events WHERE type='attempt.finished' "
            'AND payload LIKE \'%"failure":"interrupted"%\''
        ).fetchone()[0]
        mutations = con.execute(
            "SELECT count(*) FROM attempts WHERE data LIKE '%same_agent_new_context%'"
        ).fetchone()[0]
    finally:
        con.close()
    assert interrupted >= 1 and mutations >= 1
    assert repos.git(repo, "show", f"aix/run/{run_id}:feature.py").returncode == 0
    assert list((repo / ".aix" / "worktrees").iterdir()) == []
    assert not (repo / ".aix" / "runs" / run_id / "orchestrator.pid").exists()
    prompts = [p.read_text() for p in (repo / ".aix" / "runs" / run_id).glob("*.prompt.txt")]
    assert any("interrupted before it finished" in p for p in prompts)


def test_resume_refuses_a_live_orchestrator_and_bad_states(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    res = runner.invoke(app, ["run", "--resume", "run_nope", "--project", str(repo)])
    assert res.exit_code == 2 and "unknown run" in res.output, (res.output, res.exception)
    both = runner.invoke(app, ["run", "goal", "--resume", "x", "--project", str(repo)])
    assert both.exit_code == 2 and "--resume takes only a run id" in both.output

    run_id = kill_mid_attempt(repo, tmp_path)
    pid_path = repo / ".aix" / "runs" / run_id / "orchestrator.pid"
    pid_path.write_text(f"{os.getpid() + 0}\n")  # this process: not "another" orchestrator
    with monkeypatch.context() as m:
        m.setattr("aix.core.orchestrator.recovery.os.getpid", lambda: 1)  # now it is "another"
        live = runner.invoke(app, ["run", "--resume", run_id, "--project", str(repo)])
    assert live.exit_code == 2 and "still being executed" in live.output

    done = runner.invoke(app, ["run", "--resume", run_id, "--project", str(repo)])
    assert done.exit_code == 0
    again = runner.invoke(app, ["run", "--resume", run_id, "--project", str(repo)])
    assert again.exit_code == 2 and "nothing to resume" in again.output
