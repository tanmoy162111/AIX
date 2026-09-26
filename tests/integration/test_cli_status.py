"""`aix status`: a snapshot of a run's tasks, and a live table that refreshes (M3.12, §23.1)."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import repos
from aix.cli.main import app
from cli_env import hermetic
from verif_env import write_fast_config

runner = CliRunner()


def write_scripts(d: Path, **overrides: dict[str, object]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    steps: dict[str, dict[str, object]] = {
        "implement": {"write_files": {"feature.py": "def retry():\n    return 3\n"}},
        "test": {"write_files": {"tests/test_feature.py": "def test_x():\n    assert True\n"}},
        "document": {"write_files": {"docs/feature.md": "# Feature\n"}},
    }
    steps.update(overrides)
    for name, step in steps.items():
        (d / f"{name}.yaml").write_text(
            yaml.safe_dump({"match": {"task_type": name}, "attempts": [step]})
        )
    return d


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic(tmp_path, monkeypatch)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    write_fast_config(proj)
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(write_scripts(tmp_path / "scripts")))
    return proj


def cli(repo: Path, *args: str) -> tuple[int, str]:
    r = runner.invoke(app, [*args, "--project", str(repo)])
    return r.exit_code, r.output


def test_status_of_a_finished_run(repo: Path) -> None:
    code, out = cli(repo, "run", "Add a retry option", "--json")
    run_id = json.loads(out)["run_id"]
    code, out = cli(repo, "status", run_id)
    assert code == 0, out
    assert f"RUN {run_id} COMPLETED" in out
    for word in ("inspect", "design", "implement", "test", "review", "document", "fake"):
        assert word in out
    assert "6/6" in out


def test_status_without_a_run_id_shows_the_latest(repo: Path) -> None:
    cli(repo, "run", "Add a retry option", "--plan-only")
    _, out = cli(repo, "run", "Fix the crash on empty config", "--plan-only", "--json")
    latest = json.loads(out)["run_id"]
    code, shown = cli(repo, "status")
    assert code == 0 and latest in shown and "PLANNED" in shown


def test_status_json_snapshot(repo: Path) -> None:
    _, out = cli(repo, "run", "Add a retry option", "--json")
    run_id = json.loads(out)["run_id"]
    code, out = cli(repo, "status", run_id, "--json")
    doc = json.loads(out)
    assert code == 0 and doc["status"] == "completed" and doc["run_id"] == run_id
    impl = next(t for t in doc["tasks"] if t["type"] == "implement")
    assert impl["status"] == "completed" and impl["agent"] == "fake" and impl["attempts"] == 1
    assert doc["counts"]["completed"] == 6


def test_status_shows_failures(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad = write_scripts(tmp_path / "bad", implement={"exit_code": 1, "stderr": "boom"})
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(bad))
    _, out = cli(repo, "run", "Add a retry option", "--json")
    run_id = json.loads(out)["run_id"]
    _, shown = cli(repo, "status", run_id)
    assert "FAILED" in shown and "agent_failure" in shown and "blocked" in shown


def test_status_errors(repo: Path) -> None:
    code, out = cli(repo, "status", "run_00000000000000000000000000")
    assert code == 2 and "unknown run" in out
    code, out = cli(repo, "status")
    assert code == 2 and "no runs" in out


def _spawn(repo: Path, script_dir: Path) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "AIX_FAKE_SCRIPTS": str(script_dir),
    }
    code = "from aix.cli.main import app; app()"
    return subprocess.Popen(
        [sys.executable, "-c", code, "run", "Add a retry option", "--project", str(repo)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _run_id(repo: Path) -> str | None:
    db = repo / ".aix" / "aix.db"
    if not db.exists():
        return None
    con = sqlite3.connect(db)
    try:
        row = con.execute("SELECT id FROM runs").fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    return None if row is None else str(row[0])


def test_watch_prints_a_new_frame_when_the_run_changes(repo: Path, tmp_path: Path) -> None:
    slow = write_scripts(
        tmp_path / "slow", implement={"sleep_s": 2.0, "write_files": {"a.py": "x\n"}}
    )
    proc = _spawn(repo, slow)
    try:
        end = time.monotonic() + 30
        run_id = None
        while run_id is None and time.monotonic() < end:
            time.sleep(0.1)
            run_id = _run_id(repo)
        assert run_id
        code, out = cli(repo, "status", run_id, "--watch", "--interval", "0.2")
        proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert code == 0, out
    frames = re.findall(r"RUN run_\w+ (\w+)", out)
    assert frames[-1] == "COMPLETED" and len(frames) >= 2, out
    assert "EXECUTING" in frames or "PLANNING" in frames
    assert frames.count("COMPLETED") == 1  # the final frame is not printed twice
