"""`--json` on the remaining commands (M7.7, PLAYBOOK §23.2)."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
from typer.testing import CliRunner

from aix.cli.main import app
from run_env import finished_run

runner = CliRunner()


async def _prepared(tmp_path: Path) -> tuple[Path, str]:
    repo, run_id, store = await finished_run(tmp_path)
    await store.close()
    return repo, run_id


def test_cancel_of_a_finished_run_reports_json(tmp_path: Path) -> None:
    repo, run_id = anyio.run(_prepared, tmp_path)
    res = runner.invoke(app, ["cancel", run_id, "--json", "--project", str(repo)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output) == {"run_id": run_id, "result": "already", "status": "completed"}


def test_agent_test_and_toggle_json(tmp_path: Path) -> None:
    repo, _ = anyio.run(_prepared, tmp_path)
    p = ["--project", str(repo)]
    assert runner.invoke(app, ["init", *p]).exit_code == 0
    res = runner.invoke(app, ["agent", "test", "fake", "--json", *p])
    assert res.exit_code == 0, res.output
    doc = json.loads(res.output)
    assert doc["agent"] == "fake" and doc["smoke"]["status"] == "completed"
    off = json.loads(runner.invoke(app, ["agent", "disable", "gemini", "--json", *p]).output)
    assert off["enabled"] is False and "gemini" not in off["agents_enabled"]
    on = json.loads(runner.invoke(app, ["agent", "enable", "gemini", "--json", *p]).output)
    assert on["enabled"] is True and "gemini" in on["agents_enabled"]
