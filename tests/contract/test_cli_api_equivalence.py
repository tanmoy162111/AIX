"""CLI and API run the same scenario to the same recorded history (PLAYBOOK §24, M9.2).

Golden G1 (simple: "Add a hello endpoint", one fake agent, the fixture repo's real toolchain) is
started once through ``aix run`` and once through ``POST /runs``, in two identical copies of the
project. The recorded events, normalized for what legitimately differs between two runs (ids,
timestamps, durations, paths, hashes), must be identical, as must what each front end reports.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from starlette.testclient import TestClient
from typer.testing import CliRunner

import repos
from aix.api.app import create_app
from aix.api.auth import Scope
from aix.cli.main import app
from aix.store.db import EventStore
from cli_env import hermetic

runner = CliRunner()
GOAL = "Add a hello endpoint"
TOKEN = "contract-token-0123456789"
ID = re.compile(r"\b(run|task|gph|att|exe|chk|dec|apv|art|evt)_[0-9A-HJKMNP-TV-Z]{26}\b")
HEX = re.compile(r"\b[0-9a-f]{40,64}\b")
ISO = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?(Z|[+-]\d\d:\d\d)?")
VOLATILE_KEYS = {
    "seq",
    "id",
    "ts",
    "pid",
    "size",
    "path",
    "duration_ms",
    "wall_ms",
    "duration_s",
    "elapsed_s",
}
VOLATILE_SUFFIXES = ("_at", "_ms", "_s", "_seconds")


def g1_scripts(dest: Path) -> Path:
    """The hello-endpoint script plus a no-frills test and document writer (the plan has both)."""
    shutil.copytree(repos.PATCHES, dest / "patches")
    shutil.copy(repos.FIXTURES / "agent_scripts" / "hello.yaml", dest / "hello.yaml")
    extra = {
        "test": {"tests/test_hello_extra.py": "def test_extra():\n    assert True\n"},
        "document": {"docs/hello.md": "# Hello\n"},
    }
    for task_type, files in extra.items():
        doc = {"match": {"task_type": task_type}, "attempts": [{"write_files": files}]}
        (dest / f"{task_type}.yaml").write_text(yaml.safe_dump(doc))
    return dest


@pytest.fixture
def two_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    hermetic(tmp_path, monkeypatch)
    # the real toolchain of the fixture repo (python, pytest, ruff), but no agent CLI
    monkeypatch.setenv(
        "PATH", os.pathsep.join([os.environ["PATH"], str(Path(sys.executable).parent)])
    )
    monkeypatch.delenv("AIX_AGENT_CONTEXT", raising=False)
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(g1_scripts(tmp_path / "scripts")))
    out = []
    for name in ("cli", "api"):
        proj = repos.materialize_sample_py(tmp_path / name)
        assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
        repos.git(proj, "add", "-A")
        repos.git(proj, "commit", "-qm", "init aix", "--allow-empty")
        out.append(proj)
    return out[0], out[1]


class Normalizer:
    """Replaces run-specific values by stable placeholders.

    Run, task and graph ids are numbered by first appearance (the plan fixes that order before any
    task runs); every other id becomes its bare prefix.
    """

    def __init__(self, *roots: Path) -> None:
        self.roots = [str(r) for r in roots]
        self.ids: dict[str, str] = {}

    def text(self, value: str) -> str:
        for root in self.roots:
            value = value.replace(root, "<ROOT>")
        value = ISO.sub("<TS>", value)
        value = HEX.sub("<HASH>", value)
        return ID.sub(self._id, value)

    def _id(self, match: re.Match[str]) -> str:
        prefix = match.group(1)
        if prefix not in ("run", "task", "gph"):
            return prefix  # attempts, artifacts, ...: numbering follows the tasks' interleaving
        same = [i for i in self.ids if i.startswith(prefix + "_")]
        return self.ids.setdefault(match.group(0), f"{prefix}#{len(same)}")

    def value(self, value: Any, key: str = "") -> Any:
        if key in VOLATILE_KEYS or key.endswith(VOLATILE_SUFFIXES):
            return "<VOLATILE>"
        if isinstance(value, dict):
            return {k: self.value(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.value(v, key) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value


async def recorded(project: Path) -> tuple[list[dict[str, Any]], Normalizer]:
    store = await EventStore.open(project / ".aix" / "aix.db")
    try:
        events = await store.events()
    finally:
        await store.close()
    norm = Normalizer(project)
    docs = [e.model_dump(mode="json", serialize_as_any=True) for e in events]
    return [norm.value(d) for d in docs], norm


def history(events: list[dict[str, Any]], task: str | None) -> list[dict[str, Any]]:
    """Events of one task (by normalized id), of the run itself (``None``), or all (``"*"``, as a
    canonical multiset)."""
    if task == "*":
        return sorted(events, key=lambda e: json.dumps(e, sort_keys=True))
    return [e for e in events if e["task_id"] == task]


def api_run(project: Path) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    tokens = {TOKEN: frozenset(Scope)}
    with TestClient(create_app(project, tokens=tokens)) as client:
        r = client.post("/runs", json={"goal": GOAL}, headers=headers)
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            view = client.get(f"/runs/{run_id}", headers=headers).json()
            if view["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.2)
        else:
            raise AssertionError("API run did not finish")
        streamed = client.get(f"/runs/{run_id}/events", headers=headers).text  # until settled
        tasks = client.get(f"/runs/{run_id}/tasks", headers=headers).json()
    events = [
        json.loads(line.removeprefix("data: "))
        for line in streamed.splitlines()
        if line.startswith("data: ")
    ]
    return run_id, {"run": view, "tasks": tasks}, events


def test_g1_through_cli_and_api_records_the_same_history(two_projects: tuple[Path, Path]) -> None:
    cli_proj, api_proj = two_projects

    result = runner.invoke(app, ["run", GOAL, "--json", "--project", str(cli_proj)])
    assert result.exit_code == 0, result.output
    cli_doc = json.loads(result.output)
    assert cli_doc["status"] == "completed"

    api_id, api_doc, streamed = api_run(api_proj)
    assert api_doc["run"]["status"] == "completed"

    cli_events, _ = anyio.run(recorded, cli_proj)
    api_events, _ = anyio.run(recorded, api_proj)

    # 1. the recorded history is the same. Tasks without dependencies run concurrently, so the
    #    global interleaving may differ; each task's own history and the run's must not.
    assert len(cli_events) == len(api_events)
    assert history(cli_events, None) == history(api_events, None)
    assert history(cli_events, "*") == history(api_events, "*")
    for task in sorted({e["task_id"] for e in cli_events if e["task_id"]}):
        assert history(cli_events, task) == history(api_events, task), task

    # 2. what the API streamed is exactly what it recorded (nothing dropped, nothing invented)
    streamed_norm = Normalizer(api_proj)
    assert [streamed_norm.value(e) for e in streamed] == api_events

    # 3. both front ends report the same outcome
    assert [t["status"] for t in cli_doc["tasks"]] == [t["status"] for t in api_doc["tasks"]]
    assert [t["type"] for t in cli_doc["tasks"]] == [t["type"] for t in api_doc["tasks"]]
    assert [t["agent_id"] for t in cli_doc["tasks"]] == [t["agent"] for t in api_doc["tasks"]]
    assert [t["attempts"] for t in cli_doc["tasks"]] == [t["attempts"] for t in api_doc["tasks"]]
    assert cli_doc["branch"] == f"aix/run/{cli_doc['run_id']}"
    assert api_doc["run"]["branch"] == f"aix/run/{api_id}"
    assert api_doc["run"]["planner"] == cli_doc["planner"]

    # 4. G1's own assertions hold on the API path too
    kinds = [e["type"] for e in api_events]
    assert kinds.count("verification.completed") >= 1 and "decision.completed" in kinds
    assert any(
        e["type"] == "artifact.created" and e["payload"]["artifact"]["type"] == "manifest"
        for e in api_events
    )
    assert repos.git(api_proj, "show", f"aix/run/{api_id}:app/handler.py").returncode == 0
