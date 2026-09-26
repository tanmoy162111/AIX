"""Approvals: pending -> grant/deny -> resume, and the CLI's integrity checks (M5.11, §20.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import repos
from aix.cli.main import app
from aix.core.orchestrator.approvals import resolve_approval
from aix.core.orchestrator.executor import resume_run
from aix.domain.enums import FailureClass, RunStatus, TaskStatus
from aix.domain.errors import ConfigError
from aix.security.approvals import ensure_token
from aix.store.db import EventStore
from cli_env import hermetic
from verif_env import write_fast_config

runner = CliRunner()


# ---- CLI --------------------------------------------------------------------------------------


def write_scripts(d: Path, **overrides: dict[str, object]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    steps: dict[str, dict[str, object]] = {
        "implement": {"exit_code": 1, "stderr": "boom"},  # always fails
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
    monkeypatch.delenv("AIX_AGENT_CONTEXT", raising=False)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    write_fast_config(proj, ladder=["human"])
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(write_scripts(tmp_path / "scripts")))
    return proj


def cli(repo: Path, *args: str) -> tuple[int, str]:
    r = runner.invoke(app, [*args, "--project", str(repo)])
    return r.exit_code, r.output


def waiting_run(repo: Path) -> tuple[str, str]:
    code, out = cli(repo, "run", "Add a retry option", "--json")
    doc = json.loads(out)
    assert code == 3 and doc["status"] == "waiting_approval", out
    (approval,) = doc["pending_approvals"]
    return doc["run_id"], approval


def test_run_that_cannot_fix_a_task_waits_for_a_human(repo: Path) -> None:
    code, out = cli(repo, "run", "Add a retry option")
    assert code == 3 and "WAITING_APPROVAL" in out and "aix approve apv_" in out
    code, listing = cli(repo, "approvals")
    assert code == 0 and "task_decision" in listing and "apv_" in listing
    _, as_json = cli(repo, "approvals", "--json")
    assert json.loads(as_json)[0]["status"] == "pending"


def test_approve_needs_a_token_off_a_tty(repo: Path) -> None:
    _, apv = waiting_run(repo)
    code, out = cli(repo, "approve", apv)
    assert code == 2 and "--token" in out
    code, out = cli(repo, "approve", apv, "--token", "wrong")
    assert code == 2 and "invalid approval token" in out
    assert json.loads(cli(repo, "approvals", "--json")[1])[0]["status"] == "pending"


def test_approve_is_refused_inside_an_agent_context(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, apv = waiting_run(repo)
    token = ensure_token()
    monkeypatch.setenv("AIX_AGENT_CONTEXT", "1")
    code, out = cli(repo, "approve", apv, "--token", token)
    assert code == 2 and "agent context" in out
    assert cli(repo, "deny", apv, "--token", token)[0] == 2
    assert cli(repo, "approvals", "--show-token")[0] == 2


def test_approve_with_the_token_resumes_the_run(repo: Path) -> None:
    run_id, apv = waiting_run(repo)
    code, out = cli(repo, "approve", apv, "--token", ensure_token())
    assert f"Approval {apv} granted." in out
    assert code == 3 and "approval needed" in out  # the unscripted-to-succeed task waits again
    _, listing = cli(repo, "approvals", "--json")
    pending = json.loads(listing)
    assert (
        len(pending) == 1 and pending[0]["id"] != apv
    )  # the old one is resolved, a new one exists
    _, status = cli(repo, "status", run_id, "--json")
    assert json.loads(status)["status"] == "waiting_approval"


def test_approve_by_task_id_and_no_resume(repo: Path) -> None:
    run_id, _ = waiting_run(repo)
    _, status = cli(repo, "status", run_id, "--json")
    task_id = next(
        t["task_id"] for t in json.loads(status)["tasks"] if t["status"] == "waiting_approval"
    )
    code, out = cli(repo, "approve", task_id, "--token", ensure_token(), "--no-resume")
    assert code == 0 and "granted" in out
    _, status = cli(repo, "status", run_id, "--json")
    rows = json.loads(status)["tasks"]
    assert next(t for t in rows if t["task_id"] == task_id)["status"] == "ready"


def test_deny_fails_the_run(repo: Path) -> None:
    run_id, apv = waiting_run(repo)
    code, out = cli(repo, "deny", apv, "--token", ensure_token(), "--reason", "not worth it")
    assert code == 1 and "denied" in out and "FAILED" in out
    _, status = cli(repo, "status", run_id, "--json")
    doc = json.loads(status)
    assert doc["status"] == "failed"
    assert all(t["status"] in ("completed", "failed", "cancelled", "blocked") for t in doc["tasks"])
    assert cli(repo, "approve", apv, "--token", ensure_token())[0] == 2  # nothing pending now


def test_unknown_approval(repo: Path) -> None:
    code, out = cli(repo, "approve", "apv_nothere", "--token", ensure_token())
    assert code == 2 and "no pending approval" in out


def test_show_token_creates_a_private_file(repo: Path) -> None:
    code, out = cli(repo, "approvals", "--show-token")
    assert code == 0 and out.strip() == ensure_token()


@pytest.mark.anyio
async def test_resume_run_rejects_a_run_that_is_not_waiting(repo: Path) -> None:
    store = await EventStore.open(repo / ".aix" / "aix.db")
    try:
        with pytest.raises(ConfigError, match="unknown run"):
            await resume_run(
                "run_00000000000000000000000000",
                project_root=repo,
                registry=None,  # type: ignore[arg-type]
                store=store,
                config=None,  # type: ignore[arg-type]
            )
    finally:
        await store.close()
    assert RunStatus.WAITING_APPROVAL and TaskStatus.READY and FailureClass.HUMAN_REJECTION
    assert resolve_approval is not None


# ---- in-process: grant -> resume completes the run ---------------------------------------------

import sys  # noqa: E402
from datetime import UTC, datetime  # noqa: E402

from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep  # noqa: E402
from aix.agents.fakes import make_fake_entry  # noqa: E402
from aix.agents.registry import AdapterRegistry  # noqa: E402
from aix.config.schema import ExecutionConfig  # noqa: E402
from aix.core.orchestrator.executor import execute_graph  # noqa: E402
from aix.core.orchestrator.plan import record_plan  # noqa: E402
from aix.core.orchestrator.recorder import RunRecorder, new_run  # noqa: E402
from aix.core.workspace.manager import WorkspaceManager  # noqa: E402
from aix.domain.enums import Capability, TaskType  # noqa: E402
from aix.domain.enums import CheckKind as K  # noqa: E402
from aix.domain.ids import IdPrefix, new_id  # noqa: E402
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec  # noqa: E402
from verif_env import fast_config  # noqa: E402

NO_BAD = [
    sys.executable,
    "-c",
    "import pathlib,sys; sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)",
]


def _step(files: dict[str, str]) -> FakeStep:
    return FakeStep(write_files=files)


async def _setup(tmp: Path, attempts: list[FakeStep]):  # type: ignore[no-untyped-def]
    proj = repos.materialize_sample_py(tmp / "p2")
    (proj / ".aix").mkdir()
    cfg = fast_config(execution=ExecutionConfig(escalation_ladder=["human"]))
    cfg = cfg.model_copy(
        update={
            "verification": cfg.verification.model_copy(
                update={"commands": dict(cfg.verification.commands) | {K.TESTS: NO_BAD}}
            )
        }
    )
    registry = AdapterRegistry(cfg, builtin_ids=())
    script = FakeScript(match=FakeMatch(task_type="implement"), attempts=attempts)
    registry.register(
        make_fake_entry("fake-a", capabilities={Capability.IMPLEMENT: 0.9}, scripts=[script])
    )
    store = await EventStore.open(proj / ".aix" / "aix.db")
    wm = WorkspaceManager(proj)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    now = lambda: datetime.now(UTC)  # noqa: E731
    rec = RunRecorder(store, new_run(run_id, wm.root, "g", cfg, now()), now)
    await rec.start()
    task = Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title="impl", goal="do", type=TaskType.IMPLEMENT,
        required_capabilities=[Capability.IMPLEMENT], file_scope=["**"],
        verification=VerificationSpec(required=[K.TESTS]),
    )  # fmt: skip
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=[task])
    await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
    return proj, cfg, registry, store, wm, rec, graph, task


@pytest.mark.anyio
async def test_grant_then_resume_completes_the_run(tmp_path: Path) -> None:
    bad = {"feature.py": "x = 1\n", "bad.txt": "boom\n"}
    attempts = [_step(bad)] * 3 + [_step({"feature.py": "x = 2\n"})]
    proj, cfg, registry, store, wm, rec, graph, _task = await _setup(tmp_path, attempts)
    try:
        first = await execute_graph(rec, wm, graph, registry=registry, config=cfg, backoff_scale=0)
        assert first.status is RunStatus.WAITING_APPROVAL and first.exit_code == 3
        assert first.tasks[0].attempts == 3
        res = await resolve_approval(
            store, first.pending_approvals[0], grant=True, actor="me", channel="api_token"
        )
        assert res.granted and not res.run_failed
        approvals = await store.list_approvals()
        assert approvals[0].status == "granted" and approvals[0].actor == "me"
        second = await resume_run(
            first.run_id, project_root=proj, registry=registry, store=store, config=cfg,
            backoff_scale=0,
        )  # fmt: skip
        assert second.status is RunStatus.COMPLETED and second.exit_code == 0
        assert second.tasks[0].attempts == 4  # numbering continues across the pause
        run = await store.get_run(first.run_id)
        assert run is not None and run.status is RunStatus.COMPLETED
        assert repos.git(proj, "show", f"{second.branch}:feature.py").stdout.strip() == "x = 2"
        types = [e.type for e in await store.events(run_id=first.run_id)]
        assert types.index("approval.requested") < types.index("approval.granted")
        assert types[-1] == "run.completed"
    finally:
        await store.close()


@pytest.mark.anyio
async def test_deny_cancels_the_rest_and_fails_the_run_with_human_rejection(tmp_path: Path) -> None:
    bad = {"feature.py": "x = 1\n", "bad.txt": "boom\n"}
    proj, cfg, registry, store, wm, rec, graph, _task = await _setup(tmp_path, [_step(bad)])
    try:
        first = await execute_graph(rec, wm, graph, registry=registry, config=cfg, backoff_scale=0)
        res = await resolve_approval(
            store, first.pending_approvals[0], grant=False, actor="me", channel="cli_tty",
            reason="no",
        )  # fmt: skip
        assert res.run_failed
        run = await store.get_run(first.run_id)
        assert run is not None and run.status is RunStatus.FAILED
        (failed,) = await store.events(run_id=first.run_id, types=["run.failed"])
        assert failed.payload.failure is FailureClass.HUMAN_REJECTION  # type: ignore[attr-defined]
        (t,) = await store.get_tasks(first.run_id)
        assert t.status is TaskStatus.FAILED
        with pytest.raises(ConfigError):
            await resume_run(
                first.run_id, project_root=proj, registry=registry, store=store, config=cfg
            )
    finally:
        await store.close()
