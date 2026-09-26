"""Plan -> route -> schedule -> attempt -> integrate with several fake agents (M3.8, §14)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig, PlannerConfig
from aix.core.orchestrator.executor import RunOutcome, RunRequest, cancel_marker, execute_run
from aix.domain.enums import Capability, FailureClass, RunStatus, TaskStatus
from aix.domain.errors import ToolFailure
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio

C = Capability
IMPL_CAPS = {
    C.IMPLEMENT: 0.9, C.DEBUG: 0.9, C.TEST: 0.9, C.RESEARCH: 0.8,
    C.DOCUMENT: 0.8, C.DESIGN: 0.5, C.REVIEW: 0.5, C.SECURITY: 0.5,
}  # fmt: skip
REV_CAPS = {C.REVIEW: 0.95, C.SECURITY: 0.9, C.DESIGN: 0.9, C.RESEARCH: 0.6, C.DOCUMENT: 0.4}


def script(task_type: str, *attempts: FakeStep) -> FakeScript:
    return FakeScript(match=FakeMatch(task_type=task_type), attempts=list(attempts))


def writer_scripts(**extra: FakeStep) -> list[FakeScript]:
    steps = {
        "implement": FakeStep(write_files={"feature.py": "def retry():\n    return 3\n"}),
        "test": FakeStep(write_files={"tests/test_feature.py": "def test_x():\n    assert True\n"}),
        "document": FakeStep(write_files={"docs/feature.md": "# Feature\n"}),
    }
    steps.update(extra)
    return [script(t, s) for t, s in steps.items()]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    proj = repos.materialize_sample_py(tmp_path / "proj")
    (proj / ".aix").mkdir()
    return proj


async def run_it(
    repo: Path,
    agents: list[tuple[str, dict[str, object]]],
    *,
    goal: str = "Add a retry option",
    cancel: anyio.Event | None = None,
    **req: object,
) -> tuple[RunOutcome, EventStore]:
    cfg = AixConfig(planner=PlannerConfig(provider="template"))
    registry = AdapterRegistry(cfg, builtin_ids=())
    for agent_id, kw in agents:
        registry.register(make_fake_entry(agent_id, **kw))  # type: ignore[arg-type]
    store = await EventStore.open(repo / ".aix" / "aix.db")
    try:
        with anyio.fail_after(60):
            outcome = await execute_run(
                RunRequest(project_root=repo, goal=goal, **req),  # type: ignore[arg-type]
                registry=registry,
                store=store,
                config=cfg,
                cancel=cancel,
            )
    except BaseException:
        await store.close()
        raise
    return outcome, store


def two_agents(**impl_scripts: FakeStep) -> list[tuple[str, dict[str, object]]]:
    return [
        ("fake-impl", {"capabilities": IMPL_CAPS, "scripts": writer_scripts(**impl_scripts)}),
        ("fake-rev", {"capabilities": REV_CAPS}),
    ]


def by_type(o: RunOutcome, type_: str):  # type: ignore[no-untyped-def]
    return next(t for t in o.tasks if t.type == type_)


async def test_happy_path_merges_every_write_task_and_uses_two_agents(repo: Path) -> None:
    outcome, store = await run_it(repo, two_agents())
    try:
        assert outcome.status is RunStatus.COMPLETED and outcome.exit_code == 0
        assert {t.status for t in outcome.tasks} == {TaskStatus.COMPLETED}
        assert [t.type for t in outcome.tasks] == [
            "inspect", "design", "implement", "test", "review", "document",
        ]  # fmt: skip
        agents = {t.agent_id for t in outcome.tasks}
        assert agents == {"fake-impl", "fake-rev"}
        assert by_type(outcome, "implement").agent_id == "fake-impl"
        assert by_type(outcome, "review").agent_id == "fake-rev"  # independent reviewer

        for path in ("feature.py", "tests/test_feature.py", "docs/feature.md"):
            assert repos.git(repo, "show", f"{outcome.branch}:{path}").returncode == 0
        assert not (repo / "feature.py").exists()  # the user's tree is untouched
        assert repos.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() != outcome.branch
        assert list((repo / ".aix" / "worktrees").iterdir()) == []

        events = await store.events(run_id=outcome.run_id)
        types = [e.type for e in events]
        assert types.count("agent.selected") == 6 and types.count("workspace.merged") == 3
        assert types.count("workspace.removed") == 6 and types[-1] == "run.completed"
        run = await store.get_run(outcome.run_id)
        assert run is not None and run.status is RunStatus.COMPLETED
    finally:
        await store.close()


async def test_replay_reproduces_projections(repo: Path) -> None:
    outcome, store = await run_it(repo, two_agents())
    try:
        live = [t.model_dump() for t in await store.get_tasks(outcome.run_id)]
        await store.rebuild_projections()
        assert [t.model_dump() for t in await store.get_tasks(outcome.run_id)] == live
    finally:
        await store.close()


async def test_failed_task_blocks_dependents_but_not_finished_work(repo: Path) -> None:
    boom = FakeStep(exit_code=1, stderr="boom", claim=None)
    outcome, store = await run_it(repo, two_agents(implement=boom))
    try:
        assert outcome.status is RunStatus.FAILED and outcome.exit_code == 1
        status = {t.type: t.status for t in outcome.tasks}
        assert status["inspect"] is status["design"] is TaskStatus.COMPLETED
        assert status["implement"] is TaskStatus.FAILED
        for t in ("test", "review", "document"):
            assert status[t] is TaskStatus.BLOCKED
        assert outcome.failure is FailureClass.AGENT_FAILURE
        assert repos.git(repo, "branch", "--list", outcome.branch).stdout.strip()
        events = await store.events(run_id=outcome.run_id, types=["run.failed"])
        assert len(events) == 1
    finally:
        await store.close()


async def test_no_eligible_agent_fails_the_task_and_blocks_the_rest(repo: Path) -> None:
    outcome, store = await run_it(repo, [("fake-design-only", {"capabilities": {C.DESIGN: 0.9}})])
    try:
        first = outcome.tasks[0]
        assert first.status is TaskStatus.FAILED and first.failure is FailureClass.NO_ELIGIBLE_AGENT
        assert {t.status for t in outcome.tasks[1:]} == {TaskStatus.BLOCKED}
        assert outcome.failure is FailureClass.NO_ELIGIBLE_AGENT and outcome.exit_code == 1
    finally:
        await store.close()


async def test_unavailable_primary_falls_back_and_records_why(repo: Path) -> None:
    agents = [
        ("fake-a", {"capabilities": IMPL_CAPS, "health": "unavailable", "health_reason": "down"}),
        ("fake-b", {"capabilities": {**IMPL_CAPS, **REV_CAPS}, "scripts": writer_scripts()}),
    ]
    outcome, store = await run_it(repo, agents)
    try:
        assert outcome.status is RunStatus.COMPLETED
        assert {t.agent_id for t in outcome.tasks} == {"fake-b"}
        selected = await store.events(run_id=outcome.run_id, types=["agent.selected"])
        for e in selected:
            assert "ineligible:fake-a:unavailable" in e.payload.reason_codes  # type: ignore[attr-defined]
    finally:
        await store.close()


async def test_read_only_task_that_writes_is_a_scope_violation(repo: Path) -> None:
    sneaky = FakeStep(write_files={"oops.txt": "x"})
    agents = [("fake-impl", {"capabilities": IMPL_CAPS, "scripts": [script("inspect", sneaky)]})]
    outcome, store = await run_it(repo, agents)
    try:
        assert outcome.tasks[0].failure is FailureClass.SCOPE_VIOLATION
        assert outcome.status is RunStatus.FAILED
        assert await store.events(run_id=outcome.run_id, types=["policy.violation"])
    finally:
        await store.close()


async def test_write_task_without_changes_fails(repo: Path) -> None:
    lazy = FakeStep(claim="All done, trust me.")
    outcome, store = await run_it(repo, two_agents(implement=lazy))
    try:
        assert by_type(outcome, "implement").failure is FailureClass.AGENT_NO_CHANGES
        assert outcome.status is RunStatus.FAILED
    finally:
        await store.close()


async def test_not_a_git_repo_records_nothing(tmp_path: Path) -> None:
    (tmp_path / ".aix").mkdir()
    with pytest.raises(ToolFailure):
        await run_it(tmp_path, two_agents())


# ---- cancellation ---------------------------------------------------------------------------


async def cancel_when_implement_runs(repo: Path, how: str) -> tuple[RunOutcome, EventStore]:
    slow = FakeStep(sleep_s=30, write_files={"feature.py": "x = 1\n"})
    cancel = anyio.Event()
    result: list[tuple[RunOutcome, EventStore]] = []

    async def go() -> None:
        result.append(await run_it(repo, two_agents(implement=slow), cancel=cancel))

    async with anyio.create_task_group() as tg:
        tg.start_soon(go)
        db = repo / ".aix" / "aix.db"
        with anyio.fail_after(30):
            while True:
                await anyio.sleep(0.1)
                if not db.exists():
                    continue
                probe = await EventStore.open(db)
                try:
                    started = await probe.events(types=["attempt.started"])
                    run_ids = {e.run_id for e in started}
                finally:
                    await probe.close()
                if len(started) >= 3 and run_ids:
                    run_id = next(iter(run_ids))
                    break
        if how == "event":
            cancel.set()
        else:
            marker = cancel_marker(repo, str(run_id))
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("cancel\n")
    return result[0]


@pytest.mark.parametrize("how", ["event", "marker"])
async def test_cancel_stops_live_agents_and_cancels_pending_tasks(repo: Path, how: str) -> None:
    outcome, store = await cancel_when_implement_runs(repo, how)
    try:
        assert outcome.status is RunStatus.CANCELLED and outcome.exit_code == 4
        status = {t.type: t.status for t in outcome.tasks}
        assert status["inspect"] is status["design"] is TaskStatus.COMPLETED
        for t in ("implement", "test", "review", "document"):
            assert status[t] is TaskStatus.CANCELLED
        impl = by_type(outcome, "implement")
        attempts = await store.get_attempts(impl.task_id)
        assert [a.status.value for a in attempts] == ["cancelled"]
        assert await store.events(run_id=outcome.run_id, types=["run.cancelled"])
        assert list((repo / ".aix" / "worktrees").iterdir()) == []
    finally:
        await store.close()
