"""Golden G9: kill mid-attempt; `aix run --resume` marks INTERRUPTED, retries, completes."""

from __future__ import annotations

import json
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


def test_g9_crash_resume(repo: Path, tmp_path: Path) -> None:
    run_id = kill_mid_attempt(repo, tmp_path)
    assert db_scalar(repo, "SELECT status FROM runs") == "executing"  # the crash left it running
    res = runner.invoke(app, ["run", "--resume", run_id, "--json", "--project", str(repo)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["status"] == "completed"
    con = sqlite3.connect(repo / ".aix" / "aix.db")
    try:
        kinds = [r[0] for r in con.execute("SELECT type FROM events ORDER BY seq")]
        interrupted = con.execute(
            "SELECT count(*) FROM events WHERE type='attempt.finished' "
            'AND payload LIKE \'%"failure":"interrupted"%\''
        ).fetchone()[0]
        retry = con.execute(
            "SELECT count(*) FROM attempts WHERE data LIKE '%same_agent_new_context%'"
        ).fetchone()[0]
    finally:
        con.close()
    assert interrupted >= 1 and retry >= 1
    assert [k for k in kinds if k != "artifact.created"][-1] == "run.completed"
    assert repos.git(repo, "show", f"aix/run/{run_id}:feature.py").returncode == 0
