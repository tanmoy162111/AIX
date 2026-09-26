"""Which stranded task states recovery repairs, and how (M7.8, PLAYBOOK §8.2, ADR-0026)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import repos
from aix.config.schema import AixConfig
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.orchestrator.recovery import INTERRUPTED_NOTE, recover_interrupted_run
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import Capability, TaskStatus, TaskType
from aix.domain.execution import Attempt
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import TaskEvent as E
from aix.domain.state import transition_task
from aix.domain.tasks import Task
from aix.store import events as ev
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio
T = TaskStatus
TO_STATE: dict[TaskStatus, list[E]] = {
    T.CREATED: [],
    T.READY: [E.DEPS_SATISFIED],
    T.RUNNING: [E.DEPS_SATISFIED, E.ASSIGN, E.START],
    T.VERIFYING: [E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED],
    T.DECIDING: [E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED, E.VERIFIED],
    T.ACCEPTED: [E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED, E.VERIFIED, E.ACCEPT],
    T.INTEGRATING: [E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED, E.VERIFIED,
                    E.ACCEPT, E.INTEGRATE],
    T.COMPLETED: [E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED, E.VERIFIED,
                  E.ACCEPT, E.INTEGRATE, E.INTEGRATED],
    T.WAITING_APPROVAL: [E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED, E.VERIFIED,
                         E.ASK_HUMAN],
}  # fmt: skip


async def stranded(tmp_path: Path, states: dict[str, tuple[TaskStatus, bool]]):  # type: ignore[no-untyped-def]
    """A run with one task per entry ``name -> (status, merged?)``, each with a live attempt."""
    repo = repos.materialize_sample_py(tmp_path / "proj")
    (repo / ".aix").mkdir()
    store = await EventStore.open(repo / ".aix" / "aix.db")
    now = lambda: datetime.now(UTC)  # noqa: E731
    run_id = new_id(IdPrefix.RUN)
    rec = RunRecorder(store, new_run(run_id, repo, "g", AixConfig(), now()), now)
    await rec.start()
    ids: dict[str, str] = {}
    for name, (status, merged) in states.items():
        task = Task(
            id=new_id(IdPrefix.TASK), run_id=run_id, title=name, goal=name, type=TaskType.IMPLEMENT,
            required_capabilities=[Capability.IMPLEMENT], file_scope=["a.py"],
        )  # fmt: skip
        ids[name] = task.id
        await rec.emit("task.created", ev.TaskCreatedPayload(task=task), task_id=task.id)
        current = task
        for event in TO_STATE[status]:
            new = transition_task(current, event)
            await rec.emit(
                "task.state_changed",
                ev.TaskStateChangedPayload(
                    from_status=current.status, to_status=new.status, event=event, reason_codes=[]
                ),
                task_id=task.id,
            )
            current = new
        if status not in (T.CREATED, T.READY, T.COMPLETED, T.WAITING_APPROVAL):
            att = Attempt(
                id=new_id(IdPrefix.ATTEMPT), task_id=task.id, number=1, agent_id="fake-a",
                workspace=repo / ".aix" / "worktrees" / "gone", base_commit="a" * 40,
            )  # fmt: skip
            await rec.emit(
                "attempt.created", ev.AttemptCreatedPayload(attempt=att),
                task_id=task.id, attempt_id=att.id,
            )  # fmt: skip
            if merged:
                await rec.emit(
                    "workspace.merged",
                    ev.WorkspaceMergedPayload(branch="b", into="aix/run/x", commit="c"),
                    task_id=task.id, attempt_id=att.id,
                )  # fmt: skip
    return repo, store, rec, ids


async def test_each_stranded_state_is_repaired_or_left_alone(tmp_path: Path) -> None:
    states = {
        "running": (T.RUNNING, False),
        "verifying": (T.VERIFYING, False),
        "deciding": (T.DECIDING, False),
        "accepted": (T.ACCEPTED, False),
        "integrating": (T.INTEGRATING, False),
        "merged": (T.INTEGRATING, True),
        "merged_accepted": (T.ACCEPTED, True),
        "ready": (T.READY, False),
        "done": (T.COMPLETED, False),
        "waiting": (T.WAITING_APPROVAL, False),
    }
    repo, store, rec, ids = await stranded(tmp_path, states)
    try:
        report = await recover_interrupted_run(rec, WorkspaceManager(repo))
        by_id = {t.id: t.status for t in await store.get_tasks(rec.run.id)}
        for name in ("running", "verifying", "deciding", "accepted", "integrating"):
            assert by_id[ids[name]] is T.READY, name
            assert report.notes[ids[name]] == INTERRUPTED_NOTE
        for name in ("merged", "merged_accepted", "done"):
            assert by_id[ids[name]] is T.COMPLETED, name
        assert by_id[ids["ready"]] is T.READY and by_id[ids["waiting"]] is T.WAITING_APPROVAL
        assert ids["ready"] not in report.notes and ids["waiting"] not in report.notes
        assert set(report.completed) == {ids["merged"], ids["merged_accepted"]}
        assert len(report.failed_attempts) == 7  # every stranded task had one live attempt
        for aid in report.failed_attempts:
            result = await store.get_result(aid)
            assert result is not None and result.failure is not None
            assert result.failure.value == "interrupted"
        again = await recover_interrupted_run(rec, WorkspaceManager(repo))  # idempotent
        assert again.failed_attempts == [] and again.requeued == [] and again.completed == []
    finally:
        await store.close()
