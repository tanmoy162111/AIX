from __future__ import annotations

from pathlib import Path

import pytest

import factories as f
import scenarios
from aix.domain.enums import (
    AttemptStatus,
    DecisionOutcome,
    RunStatus,
    TaskStatus,
)
from aix.store.db import EventStore
from aix.store.events import (
    RunCreatedPayload,
    RunStateChangedPayload,
    TaskCreatedPayload,
    TaskStateChangedPayload,
)
from aix.store.projections import PROJECTION_TABLES

pytestmark = pytest.mark.anyio


async def _store(tmp_path: Path) -> EventStore:
    return await EventStore.open(tmp_path / "aix.db")


async def _snapshot(store: EventStore) -> dict[str, list[tuple[object, ...]]]:
    return {t: await store.dump_table(t) for t in PROJECTION_TABLES}


def test_projection_tables_match_spec() -> None:
    assert set(PROJECTION_TABLES) == {
        "runs", "tasks", "attempts", "checks", "decisions", "approvals", "artifacts", "agent_stats",
    }  # fmt: skip


async def test_full_run_is_projected(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        rid = await scenarios.play_full_run(store)
        run = await store.get_run(rid)
        assert run is not None
        assert run.status is RunStatus.COMPLETED
        assert run.finished_at is not None
        assert run.intent is not None and run.graph_id is not None

        tasks = await store.get_tasks(rid)
        assert {t.title: t.status for t in tasks} == {
            "impl": TaskStatus.COMPLETED,
            "test": TaskStatus.CREATED,
        }

        impl = next(t for t in tasks if t.title == "impl")
        attempts = await store.get_attempts(impl.id)
        assert [a.status for a in attempts] == [AttemptStatus.FAILED, AttemptStatus.COMPLETED]
        assert [a.number for a in attempts] == [1, 2]

        assert [c.status for c in await store.get_checks(attempts[0].id)] == ["failed"]
        report = await store.get_verification(attempts[1].id)
        assert report is not None and report.overall == "passed"
        result = await store.get_result(attempts[0].id)
        assert result is not None and result.usage.cost_usd == 0.01

        decisions = await store.get_decisions(rid)
        assert [d.outcome for d in decisions] == [DecisionOutcome.RETRY, DecisionOutcome.ACCEPT]

        approvals = await store.list_approvals()
        assert [a.status for a in approvals] == ["granted"]
        assert approvals[0].actor == "tanmoy"
        assert len(await store.get_artifacts(rid)) == 1
        assert [r.id for r in await store.list_runs()] == [rid]
    finally:
        await store.close()


async def test_projection_and_event_commit_together(tmp_path: Path) -> None:
    """A failing projection (duplicate run id) must not leave its event behind."""
    store = await _store(tmp_path)
    try:
        run = f.run()
        await store.append("run.created", RunCreatedPayload(run=run), run_id=run.id)
        with pytest.raises(Exception, match="UNIQUE"):
            await store.append("run.created", RunCreatedPayload(run=run), run_id=run.id)
        assert await store.count_events() == 1
        # the store is still usable afterwards
        await store.append(
            "run.state_changed",
            RunStateChangedPayload(from_status=RunStatus.CREATED, to_status=RunStatus.PLANNING),
            run_id=run.id,
        )
        assert (await store.get_run(run.id)).status is RunStatus.PLANNING  # type: ignore[union-attr]
    finally:
        await store.close()


async def test_events_about_missing_rows_are_ignored(tmp_path: Path) -> None:
    """Replay must never crash on an event whose subject row is absent."""
    store = await _store(tmp_path)
    try:
        run = f.run()
        t = f.task(run.id)
        await store.append("task.created", TaskCreatedPayload(task=t), run_id=run.id, task_id=t.id)
        ghost = f.task(run.id)
        await store.append(
            "task.state_changed",
            TaskStateChangedPayload(from_status=TaskStatus.CREATED, to_status=TaskStatus.READY),
            run_id=run.id,
            task_id=ghost.id,
        )
        assert [x.status for x in await store.get_tasks(run.id)] == [TaskStatus.CREATED]
        assert await store.rebuild_projections() == 2
    finally:
        await store.close()


async def test_rebuild_projections_equals_live_projections(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        await scenarios.play_full_run(store)
        await scenarios.play_full_run(store)
        before = await _snapshot(store)
        assert any(rows for rows in before.values())
        n = await store.rebuild_projections()
        assert n == await store.count_events()
        assert await _snapshot(store) == before
        # idempotent
        await store.rebuild_projections()
        assert await _snapshot(store) == before
    finally:
        await store.close()


async def test_rebuild_repairs_a_corrupted_projection(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    try:
        rid = await scenarios.play_full_run(store)
        good = await _snapshot(store)
        await store.execute_raw("UPDATE runs SET status = 'failed'")
        await store.execute_raw("DELETE FROM tasks")
        assert await _snapshot(store) != good
        await store.rebuild_projections()
        assert await _snapshot(store) == good
        assert (await store.get_run(rid)).status is RunStatus.COMPLETED  # type: ignore[union-attr]
    finally:
        await store.close()


async def test_rebuild_survives_reopen(tmp_path: Path) -> None:
    store = await _store(tmp_path)
    await scenarios.play_full_run(store)
    before = await _snapshot(store)
    await store.close()
    again = await _store(tmp_path)
    try:
        await again.rebuild_projections()
        assert await _snapshot(again) == before
    finally:
        await again.close()
