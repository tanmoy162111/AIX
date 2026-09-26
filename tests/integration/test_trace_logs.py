"""`aix trace` and `aix logs` (M7.7, PLAYBOOK §22, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from aix.cli.main import app
from aix.observability.trace import build_trace
from run_env import finished_run

pytestmark = pytest.mark.anyio
runner = CliRunner()


async def _prepared(tmp_path: Path) -> tuple[Path, str]:
    repo, run_id, store = await finished_run(tmp_path)
    await store.close()
    return repo, run_id


async def test_trace_tree_has_tasks_attempts_checks_and_decisions(tmp_path: Path) -> None:
    _, run_id, store = await finished_run(tmp_path)
    try:
        root = await build_trace(store, run_id)
        assert root.kind == "run" and root.status == "completed" and root.duration_ms is not None
        (task,) = root.children
        attempt = task.children[0]
        assert task.kind == "task" and attempt.kind == "attempt"
        assert attempt.label.startswith("attempt #1 fake-a")
        assert "check" in [c.kind for c in attempt.children]
        assert any(c.kind == "decision" for c in task.children + attempt.children)
        assert attempt.duration_ms is not None
        with pytest.raises(LookupError):
            await build_trace(store, "run_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    finally:
        await store.close()


def test_cli_trace_and_logs(tmp_path: Path) -> None:
    repo, run_id = anyio.run(_prepared, tmp_path)
    p = ["--project", str(repo)]
    tree = runner.invoke(app, ["trace", run_id, *p])
    assert tree.exit_code == 0 and "attempt #1" in tree.output and "task_completion" in tree.output
    doc = json.loads(runner.invoke(app, ["trace", run_id, "--json", *p]).output)
    assert doc["kind"] == "run" and doc["children"][0]["kind"] == "task"

    out = runner.invoke(app, ["logs", run_id, *p])
    assert out.exit_code == 0 and "run.created" in out.output and "run.completed" in out.output
    lines = runner.invoke(app, ["logs", run_id, "--json", *p]).output.splitlines()
    events = [json.loads(line) for line in lines]
    assert events[0]["type"] == "run.created" and all("payload" in e for e in events)
    task_id = doc["children"][0]["id"]
    only = runner.invoke(app, ["logs", run_id, "--task", task_id, "--json", *p]).output.splitlines()
    assert only and all(json.loads(x)["task_id"] == task_id for x in only)
    followed = runner.invoke(app, ["logs", run_id, "--follow", *p])  # finished run: returns at once
    assert followed.exit_code == 0 and "run.completed" in followed.output


def test_unknown_run_is_a_usage_error(tmp_path: Path) -> None:
    repo, _ = anyio.run(_prepared, tmp_path)
    for cmd in ("trace", "logs"):
        res = runner.invoke(app, [cmd, "run_nope", "--project", str(repo)])
        assert res.exit_code == 2 and "unknown run" in res.output
