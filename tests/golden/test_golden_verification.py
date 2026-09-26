"""Golden scenarios G1 (simple), G6 (scope violation), G7 (secret leak), PLAYBOOK §28.

These run the real toolchain of the fixture repository (compileall, pytest, ruff) through the
verification engine; only the agent is fake.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig, PlannerConfig
from aix.core.orchestrator.executor import RunOutcome, execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import (
    Capability,
    FailureClass,
    RunStatus,
    TaskStatus,
    TaskType,
)
from aix.domain.enums import (
    CheckKind as K,
)
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio
SCRIPTS = repos.FIXTURES / "agent_scripts"
REQUIRED = [K.BUILD, K.TESTS, K.LINT]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    proj = repos.materialize_sample_py(tmp_path / "proj")
    (proj / ".aix").mkdir()
    return proj


async def run_one(
    repo: Path, step: FakeStep, *, scope: list[str]
) -> tuple[RunOutcome, EventStore, str]:
    """One write task done by a fake agent, verified with the real toolchain."""
    cfg = AixConfig(planner=PlannerConfig(provider="template"))
    registry = AdapterRegistry(cfg, builtin_ids=())
    script = FakeScript(match=FakeMatch(task_type="implement"), attempts=[step])
    registry.register(
        make_fake_entry(
            "fake-a", base_dir=SCRIPTS, scripts=[script], capabilities={Capability.IMPLEMENT: 0.9}
        )
    )
    store = await EventStore.open(repo / ".aix" / "aix.db")
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    base = (await wm.create_run_branch(run_id)).base_commit
    now = lambda: datetime.now(UTC)  # noqa: E731
    rec = RunRecorder(store, new_run(run_id, wm.root, "Add a hello endpoint", cfg, now()), now)
    await rec.start()
    task = Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title="implement", goal="Add a hello endpoint",
        type=TaskType.IMPLEMENT, required_capabilities=[Capability.IMPLEMENT], file_scope=scope,
        verification=VerificationSpec(required=REQUIRED), max_attempts=1,
    )  # fmt: skip
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=[task])
    await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
    with anyio.fail_after(120):
        outcome = await execute_graph(rec, wm, graph, registry=registry, config=cfg)
    return outcome, store, base


async def test_g1_simple_hello_endpoint(repo: Path) -> None:
    outcome, store, _ = await run_one(
        repo, FakeStep(apply_patch="patches/hello.diff"), scope=["**"]
    )
    try:
        assert outcome.status is RunStatus.COMPLETED and outcome.tasks[0].attempts == 1
        (event,) = await store.events(run_id=outcome.run_id, types=["verification.completed"])
        report = event.payload.report  # type: ignore[attr-defined]
        assert report.overall == "passed"
        kinds = {c.kind: c for c in report.checks}
        assert {K.BUILD, K.TESTS, K.LINT, K.POLICY, K.SECRETS} <= set(kinds)
        assert kinds[K.TESTS].metrics["tests_total"] > 5  # baseline 5 + the new test(s)
        assert kinds[K.TESTS].metrics["tests_failed"] == 0
        assert kinds[K.BUILD].command and kinds[K.SECRETS].status == "passed"
        finished = await store.events(run_id=outcome.run_id, types=["check.finished"])
        assert len(finished) == len(report.checks)
        assert repos.git(repo, "show", f"{outcome.branch}:app/handler.py").returncode == 0
    finally:
        await store.close()


async def test_g6_scope_violation_is_rejected_and_never_merged(repo: Path) -> None:
    outcome, store, base = await run_one(
        repo, FakeStep(apply_patch="patches/outside_scope.diff"), scope=["app/**"]
    )
    try:
        task = outcome.tasks[0]
        assert task.status is TaskStatus.FAILED and task.failure is FailureClass.SCOPE_VIOLATION
        violations = await store.events(run_id=outcome.run_id, types=["policy.violation"])
        assert violations and violations[0].payload.kind == "scope"  # type: ignore[attr-defined]
        assert repos.git(repo, "rev-parse", outcome.branch).stdout.strip() == base  # nothing merged
        assert outcome.status is RunStatus.FAILED
    finally:
        await store.close()


async def test_g7_secret_leak_is_rejected_and_the_key_never_reaches_artifacts(repo: Path) -> None:
    key = "AKIAIOSFODNN7EXAMPLE"
    outcome, store, base = await run_one(
        repo,
        FakeStep(apply_patch="patches/planted_secret.diff", claim=f"added {key}"),
        scope=["**"],
    )
    try:
        task = outcome.tasks[0]
        assert task.status is TaskStatus.FAILED
        assert task.failure is FailureClass.VERIFICATION_FAILURE
        (event,) = await store.events(run_id=outcome.run_id, types=["verification.completed"])
        report = event.payload.report  # type: ignore[attr-defined]
        secrets = next(c for c in report.checks if c.kind is K.SECRETS)
        assert report.overall == "failed" and secrets.status == "failed"
        assert secrets.severity == "critical" and secrets.required
        assert repos.git(repo, "rev-parse", outcome.branch).stdout.strip() == base  # not merged
    finally:
        await store.close()
    # the raw key must not appear in any file aiX wrote, nor in the event log
    leaked = [
        p for p in (repo / ".aix").rglob("*") if p.is_file() and key.encode() in p.read_bytes()
    ]
    assert leaked == [], leaked
    assert (repo / ".aix" / "runs").exists()  # the patch artifact exists, redacted
