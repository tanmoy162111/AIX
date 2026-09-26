"""`aix run` (routed, multi-task), `aix cancel` and SIGINT through the real CLI (M3.8, §23)."""

from __future__ import annotations

import json
import os
import re
import signal
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


def test_routed_run_completes_and_lands_on_the_run_branch(repo: Path) -> None:
    code, out = cli(repo, "run", "Add a retry option")
    assert code == 0, out
    assert "COMPLETED" in out and "Tasks        6/6 completed" in out
    branch = re.search(r"aix/run/run_[0-9A-Z]{26}", out)
    assert branch, out
    for path in ("feature.py", "tests/test_feature.py", "docs/feature.md"):
        assert repos.git(repo, "show", f"{branch.group(0)}:{path}").returncode == 0
    assert f"Next: git merge {branch.group(0)}" in out
    assert not (repo / "feature.py").exists()


def test_json_output_and_max_parallel(repo: Path) -> None:
    code, out = cli(repo, "run", "Add a retry option", "--max-parallel", "1", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["status"] == "completed" and doc["exit_code"] == 0
    assert doc["planner"] == "template" and len(doc["tasks"]) == 6
    assert {t["status"] for t in doc["tasks"]} == {"completed"}
    assert doc["failure"] is None


def test_failed_run_exits_1_and_names_the_failures(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(
        "AIX_FAKE_SCRIPTS",
        str(write_scripts(tmp_path / "bad", implement={"exit_code": 1, "stderr": "boom"})),
    )
    code, out = cli(repo, "run", "Add a retry option")
    assert code == 1, out
    assert "FAILED" in out and "implement" in out and "blocked" in out
    assert "Failure" in out


def test_cancel_a_planned_run_directly(repo: Path) -> None:
    _, out = cli(repo, "run", "Add a retry option", "--plan-only", "--json")
    run_id = json.loads(out)["run_id"]
    code, out = cli(repo, "cancel", run_id)
    assert code == 0 and "cancelled" in out
    _, shown = cli(repo, "plan", "show", run_id, "--json")
    doc = json.loads(shown)
    assert doc["status"] == "cancelled"
    assert {t["status"] for t in doc["tasks"]} == {"cancelled"}
    code, out = cli(repo, "cancel", run_id)
    assert code == 0 and "already" in out


def test_cancel_unknown_run(repo: Path) -> None:
    code, out = cli(repo, "cancel", "run_00000000000000000000000000")
    assert code == 2 and "unknown run" in out


def _spawn(repo: Path, tmp_path: Path, script_dir: Path) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "AIX_FAKE_SCRIPTS": str(script_dir),
    }
    code = "import sys; from aix.cli.main import app; app()"
    return subprocess.Popen(
        [sys.executable, "-c", code, "run", "Add a retry option", "--project", str(repo)],
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _wait_for(pred, timeout: float = 30.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("condition not reached in time")


def _attempts_started(repo: Path) -> int:
    import sqlite3

    db = repo / ".aix" / "aix.db"
    if not db.exists():
        return 0
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT count(*) FROM events WHERE type='attempt.started'").fetchone()[0]
    except sqlite3.Error:
        return 0
    finally:
        con.close()


@pytest.mark.parametrize("how", ["sigint", "aix_cancel"])
def test_live_cancellation(repo: Path, tmp_path: Path, how: str) -> None:
    slow = write_scripts(
        tmp_path / "slow", implement={"sleep_s": 60, "write_files": {"a.py": "x\n"}}
    )
    proc = _spawn(repo, tmp_path, slow)
    try:
        _wait_for(lambda: _attempts_started(repo) >= 3)
        if how == "sigint":
            proc.send_signal(signal.SIGINT)
        else:
            import sqlite3

            con = sqlite3.connect(repo / ".aix" / "aix.db")
            run_id = con.execute("SELECT id FROM runs").fetchone()[0]
            con.close()
            code, out = cli(repo, "cancel", run_id)
            assert code == 0 and "cancelled" in out, out
        out, _ = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 4, out
    assert "CANCELLED" in out
    assert list((repo / ".aix" / "worktrees").iterdir()) == []
