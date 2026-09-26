"""Helpers for crash-recovery tests: fake scripts, a killable `aix run`, and DB probes."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import yaml


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


def db_scalar(repo: Path, sql: str) -> object:
    con = sqlite3.connect(repo / ".aix" / "aix.db")
    try:
        return con.execute(sql).fetchone()[0]
    except sqlite3.Error:
        return None
    finally:
        con.close()


def wait_for(pred, timeout: float = 30.0) -> None:  # type: ignore[no-untyped-def]
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        time.sleep(0.1)
    raise AssertionError("condition not reached in time")


def spawn(repo: Path, script_dir: Path) -> subprocess.Popen[str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"],
        "AIX_FAKE_SCRIPTS": str(script_dir),
    }
    code = "import sys; from aix.cli.main import app; app()"
    return subprocess.Popen(
        [sys.executable, "-c", code, "run", "Add a retry option", "--project", str(repo)],
        env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )  # fmt: skip


def kill_mid_attempt(repo: Path, tmp_path: Path) -> str:
    slow = write_scripts(
        tmp_path / "slow", implement={"sleep_s": 120, "write_files": {"a.py": "x\n"}}
    )
    proc = spawn(repo, slow)
    try:
        wait_for(
            lambda: (
                (db_scalar(repo, "SELECT count(*) FROM events WHERE type='attempt.started'") or 0)
                >= 3
            )  # type: ignore[operator]
        )
        proc.send_signal(signal.SIGKILL)  # no cleanup of any kind
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
    run_id = db_scalar(repo, "SELECT id FROM runs")
    assert isinstance(run_id, str)
    return run_id
