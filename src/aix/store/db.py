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

from aix.domain.errors import StoreError
from aix.domain.ids import IdPrefix, new_id
from aix.store import migrations
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
        async with self._conn.execute("SELECT COUNT(*) FROM events") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

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
        body = payload.model_dump_json()
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
                await self._conn.execute("COMMIT")
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
        assert seq is not None
        return Event(
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
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._decode(row) for row in rows]

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
