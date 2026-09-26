"""Projections: read models derived from events (PLAYBOOK §8.1-8.2).

``apply`` runs inside the same transaction as the event insert, so a projection can never be ahead
of or behind the event log. ``EventStore.rebuild_projections`` clears every table and replays
all events through ``apply``; the result must equal the live projections (tested).

Handlers are tolerant: an event about a row that does not exist is ignored so replay cannot crash
on a partial history.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import aiosqlite

from aix.domain.artifacts import Artifact
from aix.domain.decisions import Approval
from aix.domain.enums import RUN_TERMINAL, AttemptStatus
from aix.domain.execution import Attempt
from aix.domain.runs import Run
from aix.domain.tasks import Task
from aix.store import events as ev

PROJECTION_TABLES: tuple[str, ...] = (
    "runs",
    "tasks",
    "attempts",
    "checks",
    "decisions",
    "approvals",
    "artifacts",
    "agent_stats",
)

Handler = Callable[[aiosqlite.Connection, ev.Event], Awaitable[None]]


async def _one(
    conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]
) -> aiosqlite.Row | None:
    async with conn.execute(sql, params) as cur:
        return await cur.fetchone()


def _payload[P: ev.Payload](event: ev.Event, cls: type[P]) -> P:
    assert isinstance(event.payload, cls), (event.type, type(event.payload))
    return cast(P, event.payload)


# ---- runs -------------------------------------------------------------------


async def _run_created(conn: aiosqlite.Connection, e: ev.Event) -> None:
    run = _payload(e, ev.RunCreatedPayload).run
    await conn.execute(
        "INSERT INTO runs(id, status, created_at, finished_at, graph_id, data) "
        "VALUES (?,?,?,?,?,?)",
        (
            run.id,
            run.status.value,
            run.created_at.isoformat(),
            run.finished_at.isoformat() if run.finished_at else None,
            run.graph_id,
            run.model_dump_json(),
        ),
    )


async def _update_run(conn: aiosqlite.Connection, run_id: str | None, **changes: Any) -> None:
    row = await _one(conn, "SELECT data FROM runs WHERE id = ?", (run_id,))
    if row is None:
        return
    run = Run.model_validate_json(row["data"]).model_copy(update=changes)
    await conn.execute(
        "UPDATE runs SET status=?, finished_at=?, graph_id=?, data=? WHERE id=?",
        (
            run.status.value,
            run.finished_at.isoformat() if run.finished_at else None,
            run.graph_id,
            run.model_dump_json(),
            run.id,
        ),
    )


async def _run_planned(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.RunPlannedPayload)
    await _update_run(conn, e.run_id, intent=p.intent, graph_id=p.graph.id)


async def _run_state_changed(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.RunStateChangedPayload)
    changes: dict[str, Any] = {"status": p.to_status}
    if p.to_status in RUN_TERMINAL:
        changes["finished_at"] = e.ts
    await _update_run(conn, e.run_id, **changes)


# ---- tasks and attempts --------------------------------------------------------------


async def _task_created(conn: aiosqlite.Connection, e: ev.Event) -> None:
    t: Task = _payload(e, ev.TaskCreatedPayload).task
    await conn.execute(
        "INSERT INTO tasks(id, run_id, status, title, seq, data) VALUES (?,?,?,?,?,?)",
        (t.id, t.run_id, t.status.value, t.title, e.seq, t.model_dump_json()),
    )


async def _task_state_changed(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.TaskStateChangedPayload)
    row = await _one(conn, "SELECT data FROM tasks WHERE id = ?", (e.task_id,))
    if row is None:
        return
    t = Task.model_validate_json(row["data"]).model_copy(update={"status": p.to_status})
    await conn.execute(
        "UPDATE tasks SET status=?, data=? WHERE id=?", (t.status.value, t.model_dump_json(), t.id)
    )


async def _attempt_created(conn: aiosqlite.Connection, e: ev.Event) -> None:
    a: Attempt = _payload(e, ev.AttemptCreatedPayload).attempt
    await conn.execute(
        "INSERT INTO attempts(id, task_id, run_id, number, agent_id, status, seq, data) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            a.id,
            a.task_id,
            e.run_id,
            a.number,
            a.agent_id,
            a.status.value,
            e.seq,
            a.model_dump_json(),
        ),
    )


async def _set_attempt_status(
    conn: aiosqlite.Connection, attempt_id: str | None, status: AttemptStatus, result: str | None
) -> None:
    row = await _one(conn, "SELECT data FROM attempts WHERE id = ?", (attempt_id,))
    if row is None:
        return
    a = Attempt.model_validate_json(row["data"]).model_copy(update={"status": status})
    if result is None:
        await conn.execute(
            "UPDATE attempts SET status=?, data=? WHERE id=?",
            (status.value, a.model_dump_json(), a.id),
        )
    else:
        await conn.execute(
            "UPDATE attempts SET status=?, data=?, result=? WHERE id=?",
            (status.value, a.model_dump_json(), result, a.id),
        )


async def _attempt_started(conn: aiosqlite.Connection, e: ev.Event) -> None:
    await _set_attempt_status(conn, e.attempt_id, AttemptStatus.RUNNING, None)


async def _attempt_finished(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.AttemptFinishedPayload)
    await _set_attempt_status(
        conn, e.attempt_id, p.status, p.result.model_dump_json() if p.result else None
    )


# ---- verification, decisions, approvals, artifacts ------------------------------------------


async def _check_finished(conn: aiosqlite.Connection, e: ev.Event) -> None:
    c = _payload(e, ev.CheckFinishedPayload).check
    await conn.execute(
        "INSERT OR REPLACE INTO checks(id, attempt_id, run_id, kind, status, seq, data) "
        "VALUES (?,?,?,?,?,?,?)",
        (c.id, e.attempt_id, e.run_id, c.kind.value, c.status, e.seq, c.model_dump_json()),
    )


async def _verification_completed(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.VerificationCompletedPayload)
    await conn.execute(
        "UPDATE attempts SET verification = ? WHERE id = ?",
        (p.report.model_dump_json(), p.report.attempt_id),
    )


async def _decision_completed(conn: aiosqlite.Connection, e: ev.Event) -> None:
    d = _payload(e, ev.DecisionCompletedPayload).record
    await conn.execute(
        "INSERT INTO decisions(id, run_id, point, subject, provider, outcome, seq, data) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            d.id,
            e.run_id,
            d.point.value,
            d.subject,
            d.provider,
            d.outcome.value,
            e.seq,
            d.model_dump_json(),
        ),
    )


async def _approval_requested(conn: aiosqlite.Connection, e: ev.Event) -> None:
    a = _payload(e, ev.ApprovalRequestedPayload).approval
    await conn.execute(
        "INSERT INTO approvals(id, run_id, subject, status, seq, data) VALUES (?,?,?,?,?,?)",
        (a.id, e.run_id, a.subject, a.status, e.seq, a.model_dump_json()),
    )


async def _update_approval(conn: aiosqlite.Connection, approval_id: str, **changes: Any) -> None:
    row = await _one(conn, "SELECT data FROM approvals WHERE id = ?", (approval_id,))
    if row is None:
        return
    a = Approval.model_validate_json(row["data"]).model_copy(update=changes)
    await conn.execute(
        "UPDATE approvals SET status=?, data=? WHERE id=?", (a.status, a.model_dump_json(), a.id)
    )


async def _approval_granted(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.ApprovalGrantedPayload)
    await _update_approval(
        conn,
        p.approval_id,
        status="granted",
        actor=p.actor,
        channel=p.channel,
        decided_at=p.decided_at,
    )


async def _approval_denied(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.ApprovalDeniedPayload)
    await _update_approval(
        conn,
        p.approval_id,
        status="denied",
        actor=p.actor,
        channel=p.channel,
        decided_at=p.decided_at,
    )


async def _approval_expired(conn: aiosqlite.Connection, e: ev.Event) -> None:
    p = _payload(e, ev.ApprovalExpiredPayload)
    await _update_approval(conn, p.approval_id, status="expired", decided_at=p.decided_at)


async def _artifact_created(conn: aiosqlite.Connection, e: ev.Event) -> None:
    a: Artifact = _payload(e, ev.ArtifactCreatedPayload).artifact
    await conn.execute(
        "INSERT INTO artifacts(id, run_id, type, sha256, seq, data) VALUES (?,?,?,?,?,?)",
        (a.id, e.run_id, a.type.value, a.sha256, e.seq, a.model_dump_json()),
    )


_HANDLERS: dict[str, Handler] = {
    "run.created": _run_created,
    "run.planned": _run_planned,
    "run.state_changed": _run_state_changed,
    "task.created": _task_created,
    "task.state_changed": _task_state_changed,
    "attempt.created": _attempt_created,
    "attempt.started": _attempt_started,
    "attempt.finished": _attempt_finished,
    "check.finished": _check_finished,
    "verification.completed": _verification_completed,
    "decision.completed": _decision_completed,
    "approval.requested": _approval_requested,
    "approval.granted": _approval_granted,
    "approval.denied": _approval_denied,
    "approval.expired": _approval_expired,
    "artifact.created": _artifact_created,
}


async def apply(conn: aiosqlite.Connection, event: ev.Event) -> None:
    """Update projections for ``event``. Must be called inside the event's transaction."""
    handler = _HANDLERS.get(event.type)
    if handler is not None:
        await handler(conn, event)


async def clear(conn: aiosqlite.Connection) -> None:
    """Delete every projection row (the event log is untouched)."""
    for table in PROJECTION_TABLES:
        await conn.execute(f"DELETE FROM {table}")
