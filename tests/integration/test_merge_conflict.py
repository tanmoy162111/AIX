"""Two parallel write tasks touch the same file: serialized merge, conflict, retry (M3.9, §14.3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
from aix.core.orchestrator.executor import RunOutcome, execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import (
    Capability,
    FailureClass,
    RetryMutation,
    RunStatus,
    TaskStatus,
    TaskType,
)
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio


def script(word: str, *steps: FakeStep) -> FakeScript:
    return FakeScript(match=FakeMatch(prompt_contains=word), attempts=list(steps))


def slow(**files: str) -> FakeStep:
    return FakeStep(sleep_s=0.4, write_files=files)  # both attempts start before either merges


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    proj = repos.materialize_sample_py(tmp_path / "proj")
    (proj / ".aix").mkdir()
    return proj


async def run_pair(
    repo: Path, *, max_attempts: int, scripts: list[FakeScript]
) -> tuple[RunOutcome, EventStore]:
    cfg = AixConfig()
    registry = AdapterRegistry(cfg, builtin_ids=())
    registry.register(make_fake_entry("fake-x", scripts=scripts))
    store = await EventStore.open(repo / ".aix" / "aix.db")
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    rec = RunRecorder(
        store,
        new_run(run_id, wm.root, "conflict", cfg, datetime.now(UTC)),
        lambda: datetime.now(UTC),
    )
    await rec.start()
    tasks = [
        Task(
            id=new_id(IdPrefix.TASK),
            run_id=run_id,
            title=name,
            goal=name,
            type=TaskType.IMPLEMENT,
            required_capabilities=[Capability.IMPLEMENT],
            file_scope=["**"],
            max_attempts=max_attempts,
        )
        for name in ("alpha", "beta")
    ]
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=tasks)
    await record_plan(rec, Intent(goal="conflict", kind="coding", risk="low"), graph, "test", [])
    with anyio.fail_after(60):
        outcome = await execute_graph(rec, wm, graph, registry=registry, config=cfg)
    return outcome, store


async def test_conflict_is_detected_and_the_loser_retries_on_the_new_head(repo: Path) -> None:
    second_a = FakeStep(write_files={"alpha2.txt": "a\n"})
    second_b = FakeStep(write_files={"beta2.txt": "b\n"})
    scripts = [
        script("alpha", slow(**{"shared.txt": "alpha\n"}), second_a),
        script("beta", slow(**{"shared.txt": "beta\n"}), second_b),
    ]
    outcome, store = await run_pair(repo, max_attempts=3, scripts=scripts)
    try:
        assert outcome.status is RunStatus.COMPLETED
        assert {t.status for t in outcome.tasks} == {TaskStatus.COMPLETED}

        conflicts = await store.events(run_id=outcome.run_id, types=["workspace.conflict"])
        assert len(conflicts) == 1 and conflicts[0].payload.files == ["shared.txt"]  # type: ignore[attr-defined]
        loser = next(t for t in outcome.tasks if t.attempts == 2)
        winner = next(t for t in outcome.tasks if t.attempts == 1)
        attempts = await store.get_attempts(loser.task_id)
        assert [a.number for a in attempts] == [1, 2]
        assert attempts[0].mutation is None
        assert attempts[1].mutation is RetryMutation.REBASE_AND_RETRY
        assert attempts[1].base_commit != attempts[0].base_commit  # rebased onto the new head

        shown = repos.git(repo, "show", f"{outcome.branch}:shared.txt").stdout.strip()
        assert shown == winner.title  # the first merge wins; the loser's first attempt is dropped
        extra = "alpha2.txt" if loser.title == "alpha" else "beta2.txt"
        assert repos.git(repo, "show", f"{outcome.branch}:{extra}").returncode == 0

        transitions = [
            (e.payload.from_status, e.payload.event)  # type: ignore[attr-defined]
            for e in await store.events(run_id=outcome.run_id, types=["task.state_changed"])
            if e.task_id == loser.task_id
        ]
        assert (TaskStatus.INTEGRATING, "merge_conflict") in [(f, str(v)) for f, v in transitions]
    finally:
        await store.close()


async def test_conflict_with_no_attempts_left_fails_the_task(repo: Path) -> None:
    scripts = [
        script("alpha", slow(**{"shared.txt": "alpha\n"})),
        script("beta", slow(**{"shared.txt": "beta\n"})),
    ]
    outcome, store = await run_pair(repo, max_attempts=1, scripts=scripts)
    try:
        assert outcome.status is RunStatus.FAILED and outcome.failure is FailureClass.MERGE_CONFLICT
        statuses = sorted(t.status.value for t in outcome.tasks)
        assert statuses == ["completed", "failed"]
        failed = next(t for t in outcome.tasks if t.status is TaskStatus.FAILED)
        assert failed.failure is FailureClass.MERGE_CONFLICT and failed.attempts == 1
        winner = next(t for t in outcome.tasks if t.status is TaskStatus.COMPLETED)
        shown = repos.git(repo, "show", f"{outcome.branch}:shared.txt").stdout.strip()
        assert shown == winner.title
    finally:
        await store.close()
