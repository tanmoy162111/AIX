"""SQLite event store (PLAYBOOK §8).

One database per project (``.aix/aix.db``), WAL mode. Appends are serialized by a lock so each
event and its projection updates (M1.7) commit in one transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

import aiosqlite
import anyio
from pydantic import BaseModel

from aix.domain.artifacts import Artifact
from aix.domain.decisions import Approval, DecisionRecord
from aix.domain.errors import StoreError
from aix.domain.execution import Attempt, ExecutionResult
from aix.domain.ids import IdPrefix, new_id
from aix.domain.runs import Run
from aix.domain.tasks import Task
from aix.domain.verification import Check, VerificationReport
from aix.security.redact import redact_secrets
from aix.store import migrations, projections
from aix.store.events import EVENT_PAYLOADS, SCHEMA_VERSION, Event


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EventStore:
    """Async facade over the project's SQLite database."""

    def __init__(self, conn: aiosqlite.Connection, clock: Callable[[], datetime]) -> None:
        self._conn = conn
        self._clock = clock
        self._lock = anyio.Lock()

    @classmethod
    async def open(cls, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> Self:
        """Open (creating if needed) the database, enable WAL and apply pending migrations.

        Raises:
            StoreError: if the database was written by a newer aix (schema version too high).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        store = cls(conn, clock)
        try:
            await conn.execute("PRAGMA journal_mode = WAL")
            await conn.execute("PRAGMA synchronous = NORMAL")
            await conn.execute("PRAGMA foreign_keys = ON")
            await conn.execute("PRAGMA busy_timeout = 5000")
            await store._migrate()
        except BaseException:
            await conn.close()
            raise
        return store

    async def _migrate(self) -> None:
        current = int(await self.pragma("user_version"))
        if current > migrations.latest_version():
            raise StoreError(
                f"database schema v{current} is newer than this aix "
                f"(v{migrations.latest_version()})"
            )
        for mig in migrations.pending(current):
            await self._conn.executescript(mig.sql)

    async def close(self) -> None:
        """Close the connection."""
        await self._conn.close()

    async def pragma(self, name: str) -> Any:
        """Return the value of ``PRAGMA <name>`` (name must be a bare identifier)."""
        if not name.replace("_", "").isalnum():
            raise ValueError(f"bad pragma name: {name!r}")
        async with self._conn.execute(f"PRAGMA {name}") as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def execute_raw(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run an arbitrary statement. Intended for tests and maintenance only."""
        async with self._lock:
            await self._conn.execute(sql, params)

    async def count_events(self) -> int:
        """Total number of stored events."""
        rows = await self._query("SELECT COUNT(*) AS n FROM events")
        return int(rows[0]["n"])

    async def append(
        self,
        event_type: str,
        payload: BaseModel,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        attempt_id: str | None = None,
        ts: datetime | None = None,
    ) -> Event:
        """Append one event and return it with its assigned ``seq``.

        Raises:
            KeyError: unknown ``event_type``.
            TypeError: ``payload`` is not the model registered for ``event_type``.
        """
        payload_cls = EVENT_PAYLOADS[event_type]
        if not isinstance(payload, payload_cls):
            raise TypeError(
                f"event {event_type!r} needs a {payload_cls.__name__}, got {type(payload).__name__}"
            )
        when = ts or self._clock()
        event_id = new_id(IdPrefix.EVENT)
        raw = payload.model_dump_json()
        body = redact_secrets(raw)
        if body != raw:  # projections must see exactly what a replay will see
            payload = payload_cls.model_validate_json(body)
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                cur = await self._conn.execute(
                    "INSERT INTO events(id, run_id, task_id, attempt_id, type, ts, payload, "
                    "schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        event_id,
                        run_id,
                        task_id,
                        attempt_id,
                        event_type,
                        when.isoformat(),
                        body,
                        SCHEMA_VERSION,
                    ),
                )
                seq = cur.lastrowid
                assert seq is not None
                event = Event(
                    seq=seq,
                    id=event_id,
                    run_id=run_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    type=event_type,
                    ts=when,
                    payload=payload,
                    schema_version=SCHEMA_VERSION,
                )
                await projections.apply(self._conn, event)
                await self._conn.execute("COMMIT")
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
        return event

    async def events(
        self,
        *,
        run_id: str | None = None,
        after_seq: int = 0,
        types: Sequence[str] | None = None,
    ) -> list[Event]:
        """Events in append order, optionally filtered.

        Raises:
            StoreError: a stored row has an unknown type or a newer payload schema version.
        """
        clauses = ["seq > ?"]
        params: list[Any] = [after_seq]
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if types:
            clauses.append(f"type IN ({', '.join('?' for _ in types)})")
            params.extend(types)
        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY seq"
        async with self._lock:
            rows = await self._fetch(sql, params)
        return [self._decode(row) for row in rows]

    async def _fetch(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        """Run a SELECT on the shared connection. Callers must hold ``self._lock``."""
        async with self._conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _query(self, sql: str, params: Sequence[Any] = ()) -> list[aiosqlite.Row]:
        async with self._lock:
            return await self._fetch(sql, params)

    async def rebuild_projections(self) -> int:
        """Clear all projections and replay every event through them in one transaction.

        Returns the number of events replayed. The event log is never modified.
        """
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                await projections.clear(self._conn)
                rows = await self._fetch("SELECT * FROM events ORDER BY seq")
                for row in rows:
                    await projections.apply(self._conn, self._decode(row))
                await self._conn.execute("COMMIT")
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
        return len(rows)

    async def dump_table(self, table: str) -> list[tuple[Any, ...]]:
        """All rows of a projection table as tuples, sorted; used to compare projections."""
        if table not in projections.PROJECTION_TABLES:
            raise ValueError(f"not a projection table: {table!r}")
        rows = await self._query(f"SELECT * FROM {table}")
        return sorted((tuple(r) for r in rows), key=repr)

    # ---- typed readers over the projections ---------------------------------------------

    async def _models[M: BaseModel](
        self, model: type[M], sql: str, params: Sequence[Any] = ()
    ) -> list[M]:
        return [model.model_validate_json(r["data"]) for r in await self._query(sql, params)]

    async def get_run(self, run_id: str) -> Run | None:
        """The run, or ``None``."""
        found = await self._models(Run, "SELECT data FROM runs WHERE id = ?", (run_id,))
        return found[0] if found else None

    async def list_runs(self) -> list[Run]:
        """All runs, oldest first."""
        return await self._models(Run, "SELECT data FROM runs ORDER BY created_at, id")

    async def get_tasks(self, run_id: str) -> list[Task]:
        """Tasks of a run in creation order."""
        return await self._models(
            Task, "SELECT data FROM tasks WHERE run_id = ? ORDER BY seq", (run_id,)
        )

    async def get_attempts(self, task_id: str) -> list[Attempt]:
        """Attempts of a task by attempt number."""
        return await self._models(
            Attempt, "SELECT data FROM attempts WHERE task_id = ? ORDER BY number", (task_id,)
        )

    async def get_result(self, attempt_id: str) -> ExecutionResult | None:
        """The recorded ``ExecutionResult`` of a finished attempt."""
        rows = await self._query("SELECT result FROM attempts WHERE id = ?", (attempt_id,))
        if not rows or rows[0]["result"] is None:
            return None
        return ExecutionResult.model_validate_json(rows[0]["result"])

    async def get_verification(self, attempt_id: str) -> VerificationReport | None:
        """The verification report of an attempt."""
        rows = await self._query("SELECT verification FROM attempts WHERE id = ?", (attempt_id,))
        if not rows or rows[0]["verification"] is None:
            return None
        return VerificationReport.model_validate_json(rows[0]["verification"])

    async def get_checks(self, attempt_id: str) -> list[Check]:
        """Checks recorded for an attempt in completion order."""
        return await self._models(
            Check, "SELECT data FROM checks WHERE attempt_id = ? ORDER BY seq", (attempt_id,)
        )

    async def get_decisions(self, run_id: str) -> list[DecisionRecord]:
        """Decisions of a run in the order they were made."""
        return await self._models(
            DecisionRecord, "SELECT data FROM decisions WHERE run_id = ? ORDER BY seq", (run_id,)
        )

    async def list_approvals(self, status: str | None = None) -> list[Approval]:
        """Approvals, optionally only those with ``status``."""
        if status is None:
            return await self._models(Approval, "SELECT data FROM approvals ORDER BY seq")
        return await self._models(
            Approval, "SELECT data FROM approvals WHERE status = ? ORDER BY seq", (status,)
        )

    async def get_artifacts(self, run_id: str) -> list[Artifact]:
        """Artifacts of a run in creation order."""
        return await self._models(
            Artifact, "SELECT data FROM artifacts WHERE run_id = ? ORDER BY seq", (run_id,)
        )

    @staticmethod
    def _decode(row: aiosqlite.Row) -> Event:
        etype = str(row["type"])
        payload_cls = EVENT_PAYLOADS.get(etype)
        if payload_cls is None:
            raise StoreError(f"unknown event type {etype!r} at seq {row['seq']}")
        if int(row["schema_version"]) > SCHEMA_VERSION:
            raise StoreError(f"event seq {row['seq']} has a newer payload schema version")
        return Event(
            seq=row["seq"],
            id=row["id"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt_id=row["attempt_id"],
            type=etype,
            ts=datetime.fromisoformat(row["ts"]),
            payload=payload_cls.model_validate_json(row["payload"]),
            schema_version=row["schema_version"],
        )
