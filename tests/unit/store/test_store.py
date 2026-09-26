from __future__ import annotations

import sqlite3
from pathlib import Path

import anyio
import pytest

import factories as f
from aix.domain.enums import RunStatus
from aix.domain.errors import StoreError
from aix.domain.ids import IdPrefix, new_id
from aix.store import migrations
from aix.store.db import EventStore
from aix.store.events import (
    ArtifactCreatedPayload,
    Event,
    RunCreatedPayload,
    RunStateChangedPayload,
)

pytestmark = pytest.mark.anyio


async def _open(tmp_path: Path) -> EventStore:
    return await EventStore.open(tmp_path / "aix.db")


def _created(run_id: str | None = None) -> RunCreatedPayload:
    return RunCreatedPayload(run=f.run(id=run_id) if run_id else f.run())


async def test_open_enables_wal_and_applies_migrations(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    try:
        assert await store.pragma("journal_mode") == "wal"
        assert await store.pragma("foreign_keys") == 1
        assert await store.pragma("user_version") == migrations.latest_version()
    finally:
        await store.close()


async def test_reopen_is_idempotent(tmp_path: Path) -> None:
    (await _open(tmp_path))
    store = await _open(tmp_path)
    await store.close()
    again = await _open(tmp_path)
    await again.close()


async def test_database_from_the_future_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "aix.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {migrations.latest_version() + 5}")
    conn.commit()
    conn.close()
    with pytest.raises(StoreError, match="newer"):
        await EventStore.open(path)


async def test_append_assigns_increasing_seq_and_round_trips(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    try:
        run = f.run()
        e1 = await store.append("run.created", RunCreatedPayload(run=run), run_id=run.id, ts=f.NOW)
        e2 = await store.append(
            "run.state_changed",
            RunStateChangedPayload(from_status=RunStatus.CREATED, to_status=RunStatus.PLANNING),
            run_id=run.id,
        )
        assert (e1.seq, e2.seq) == (1, 2)
        got = await store.events(run_id=run.id)
        assert [e.type for e in got] == ["run.created", "run.state_changed"]
        assert got[0] == e1
        assert isinstance(got[0].payload, RunCreatedPayload)
        assert got[0].payload.run == run
        assert got[0].ts == f.NOW
    finally:
        await store.close()


async def test_events_are_append_only(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    try:
        run = f.run()
        await store.append("run.created", RunCreatedPayload(run=run), run_id=run.id)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            await store.execute_raw("UPDATE events SET type = 'x'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            await store.execute_raw("DELETE FROM events")
        assert await store.count_events() == 1
    finally:
        await store.close()


async def test_payload_must_match_event_type(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    try:
        with pytest.raises(TypeError, match=r"run.planned"):
            await store.append("run.planned", _created())
        with pytest.raises(KeyError, match=r"not.a.type"):
            await store.append("not.a.type", _created())
        assert await store.count_events() == 0
    finally:
        await store.close()


async def test_filters_by_run_after_seq_and_type(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    try:
        a, b = f.run(), f.run()
        await store.append("run.created", _created(a.id), run_id=a.id)
        await store.append("run.created", _created(b.id), run_id=b.id)
        art = ArtifactCreatedPayload(artifact=f.artifact(a.id))
        await store.append("artifact.created", art, run_id=a.id)
        assert len(await store.events(run_id=a.id)) == 2
        assert len(await store.events(run_id=b.id)) == 1
        assert [e.seq for e in await store.events(after_seq=1)] == [2, 3]
        assert [e.type for e in await store.events(types=["artifact.created"])] == [
            "artifact.created"
        ]
        assert await store.events(run_id=new_id(IdPrefix.RUN)) == []
    finally:
        await store.close()


async def test_concurrent_appends_get_unique_contiguous_seq(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    seen: list[Event] = []
    try:

        async def one() -> None:
            run = f.run()
            seen.append(await store.append("run.created", _created(run.id), run_id=run.id))

        async with anyio.create_task_group() as tg:
            for _ in range(40):
                tg.start_soon(one)
        assert sorted(e.seq for e in seen) == list(range(1, 41))
    finally:
        await store.close()


async def test_unknown_stored_type_is_a_store_error(tmp_path: Path) -> None:
    store = await _open(tmp_path)
    try:
        await store.execute_raw(
            "INSERT INTO events(id, type, ts, payload, schema_version) "
            "VALUES ('evt_x', 'bogus.type', '2026-01-01T00:00:00+00:00', '{}', 1)"
        )
        with pytest.raises(StoreError, match=r"bogus.type"):
            await store.events()
    finally:
        await store.close()


async def test_injected_clock_is_used(tmp_path: Path) -> None:
    store = await EventStore.open(tmp_path / "aix.db", clock=lambda: f.NOW)
    try:
        e = await store.append("run.created", _created())
        assert e.ts == f.NOW
    finally:
        await store.close()


async def test_secrets_are_redacted_before_storage_and_projection(tmp_path: Path) -> None:
    from aix.store.events import AgentOutputPayload

    key = "AKIAIOSFODNN7EXAMPLE"
    store = await _open(tmp_path)
    try:
        run = f.run()
        await store.append("run.created", RunCreatedPayload(run=run), run_id=run.id)
        ev = await store.append(
            "agent.output", AgentOutputPayload(text=f"key is {key}"), run_id=run.id
        )
        assert key not in ev.payload.text  # type: ignore[attr-defined]
        (stored,) = await store.events(run_id=run.id, types=["agent.output"])
        assert stored.payload.text == "key is [REDACTED]"  # type: ignore[attr-defined]
    finally:
        await store.close()
    assert key.encode() not in (tmp_path / "aix.db").read_bytes()
