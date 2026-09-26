"""Reusable event sequences that exercise every projection (used by store tests)."""

from __future__ import annotations

from datetime import timedelta

import factories as f
from aix.domain.enums import (
    AttemptStatus,
    DecisionOutcome,
    RunStatus,
    TaskStatus,
)
from aix.domain.execution import Usage
from aix.domain.state import RunEvent, TaskEvent
from aix.store import events as ev
from aix.store.db import EventStore


async def play_full_run(store: EventStore) -> str:
    """Append a plausible complete run (one failed attempt, one accepted) and return its run id."""
    run = f.run()
    rid = run.id
    step = 0

    def at() -> object:
        nonlocal step
        step += 1
        return f.NOW + timedelta(seconds=step)

    async def emit(etype: str, payload: object, **ids: str) -> None:
        await store.append(etype, payload, run_id=rid, ts=at(), **ids)  # type: ignore[arg-type]

    await emit("run.created", ev.RunCreatedPayload(run=run))
    await emit(
        "run.state_changed",
        ev.RunStateChangedPayload(
            from_status=RunStatus.CREATED,
            to_status=RunStatus.PLANNING,
            event=RunEvent.START_PLANNING,
        ),
    )
    t1 = f.task(rid, title="impl")
    t2 = f.task(rid, title="test", depends_on=[t1.id])
    graph = f.graph(rid, [t1, t2])
    await emit("run.planned", ev.RunPlannedPayload(intent=f.intent(), graph=graph))
    for t in (t1, t2):
        await emit("task.created", ev.TaskCreatedPayload(task=t), task_id=t.id)
    await emit(
        "run.state_changed",
        ev.RunStateChangedPayload(from_status=RunStatus.PLANNING, to_status=RunStatus.EXECUTING),
    )

    async def task_to(task_id: str, frm: TaskStatus, to: TaskStatus, e: TaskEvent) -> None:
        await emit(
            "task.state_changed",
            ev.TaskStateChangedPayload(from_status=frm, to_status=to, event=e),
            task_id=task_id,
        )

    await task_to(t1.id, TaskStatus.CREATED, TaskStatus.READY, TaskEvent.DEPS_SATISFIED)
    for n, agent in ((1, "fake-a"), (2, "fake-b")):
        att = f.attempt(t1.id, number=n, agent_id=agent)
        await emit(
            "attempt.created",
            ev.AttemptCreatedPayload(attempt=att),
            task_id=t1.id,
            attempt_id=att.id,
        )
        await emit(
            "attempt.started",
            ev.AttemptStartedPayload(pid=100 + n),
            task_id=t1.id,
            attempt_id=att.id,
        )
        ok = n == 2
        chk = f.check(status="passed" if ok else "failed")
        await emit(
            "check.finished", ev.CheckFinishedPayload(check=chk), task_id=t1.id, attempt_id=att.id
        )
        await emit(
            "verification.completed",
            ev.VerificationCompletedPayload(
                report=f.report(att.id, [chk], "passed" if ok else "failed")
            ),
            task_id=t1.id,
            attempt_id=att.id,
        )
        res = f.execution_result(
            att.id, usage=Usage(input_tokens=10, output_tokens=5, cost_usd=0.01)
        )
        await emit(
            "attempt.finished",
            ev.AttemptFinishedPayload(
                status=AttemptStatus.COMPLETED if ok else AttemptStatus.FAILED, result=res
            ),
            task_id=t1.id,
            attempt_id=att.id,
        )
        dec = f.decision(
            subject=att.id,
            outcome=DecisionOutcome.ACCEPT if ok else DecisionOutcome.RETRY,
            reason_codes=["rules:ok" if ok else "rules:retry"],
        )
        await emit(
            "decision.completed",
            ev.DecisionCompletedPayload(record=dec),
            task_id=t1.id,
            attempt_id=att.id,
        )
    await task_to(t1.id, TaskStatus.READY, TaskStatus.COMPLETED, TaskEvent.INTEGRATED)

    appr = f.approval(subject=t2.id)
    await emit("approval.requested", ev.ApprovalRequestedPayload(approval=appr), task_id=t2.id)
    await emit(
        "approval.granted",
        ev.ApprovalGrantedPayload(
            approval_id=appr.id,
            actor="tanmoy",
            channel="cli_tty",
            decided_at=f.NOW + timedelta(hours=1),
        ),
        task_id=t2.id,
    )
    await emit("artifact.created", ev.ArtifactCreatedPayload(artifact=f.artifact(rid)))
    await emit(
        "run.state_changed",
        ev.RunStateChangedPayload(from_status=RunStatus.EXECUTING, to_status=RunStatus.COMPLETED),
    )
    return rid
