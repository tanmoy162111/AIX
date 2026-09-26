"""Resolving approvals: record the human's decision and move tasks and the run (PLAYBOOK §20.4)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from aix.domain.decisions import Approval
from aix.domain.enums import FailureClass, RunStatus, TaskStatus
from aix.domain.errors import ConfigError
from aix.domain.state import (
    TASK_TERMINAL,
    RunEvent,
    TaskEvent,
    transition_run,
    transition_task,
)
from aix.store import events as ev
from aix.store.db import EventStore

Channel = Literal["cli_tty", "api_token"]


@dataclass(frozen=True)
class Resolution:
    approval: Approval
    granted: bool
    run_id: str
    run_failed: bool = False
    """Denial ends the run (§28 G4)."""


def _utc() -> datetime:
    return datetime.now(UTC)


async def find_approval(store: EventStore, ref: str) -> tuple[Approval, str]:
    """A pending approval by its id or by its subject (task or run id), with its run id.

    Raises:
        ConfigError: nothing pending matches.
    """
    pending = await store.list_approvals("pending")
    matches = [a for a in pending if a.id == ref or a.subject == ref]
    if not matches:
        raise ConfigError(f"no pending approval matches {ref!r}")
    approval = matches[0]
    for e in await store.events(types=["approval.requested"]):
        payload = e.payload
        if isinstance(payload, ev.ApprovalRequestedPayload) and payload.approval.id == approval.id:
            assert e.run_id is not None
            return approval, e.run_id
    raise ConfigError(f"approval {approval.id} has no recorded run")


async def resolve_approval(
    store: EventStore,
    ref: str,
    *,
    grant: bool,
    actor: str,
    channel: Channel,
    reason: str | None = None,
    clock: Callable[[], datetime] = _utc,
) -> Resolution:
    """Grant or deny a pending approval.

    Grant: the approval becomes ``granted`` and a task waiting on it becomes ``ready``; the run
    stays ``waiting_approval`` until it is resumed. Deny: the approval becomes ``denied``, the
    task fails (``HUMAN_REJECTION``), every other non-terminal task is cancelled and the run
    fails.

    Raises:
        ConfigError: nothing pending matches ``ref``.
    """
    approval, run_id = await find_approval(store, ref)
    now = clock()
    if grant:
        await store.append(
            "approval.granted",
            ev.ApprovalGrantedPayload(
                approval_id=approval.id, actor=actor, channel=channel, decided_at=now
            ),
            run_id=run_id,
            ts=now,
        )
    else:
        await store.append(
            "approval.denied",
            ev.ApprovalDeniedPayload(
                approval_id=approval.id,
                actor=actor,
                channel=channel,
                decided_at=now,
                reason=reason,
            ),
            run_id=run_id,
            ts=now,
        )
    tasks = await store.get_tasks(run_id)
    run = await store.get_run(run_id)
    assert run is not None

    async def move(task_id: str, event: TaskEvent, *codes: str) -> None:
        task = next(t for t in await store.get_tasks(run_id) if t.id == task_id)
        new = transition_task(task, event)
        await store.append(
            "task.state_changed",
            ev.TaskStateChangedPayload(
                from_status=task.status,
                to_status=new.status,
                event=event,
                reason_codes=list(codes),
            ),
            run_id=run_id,
            task_id=task_id,
            ts=clock(),
        )

    waiting = next(
        (t for t in tasks if t.id == approval.subject and t.status is TaskStatus.WAITING_APPROVAL),
        None,
    )
    if grant:
        if waiting is not None:
            await move(waiting.id, TaskEvent.APPROVAL_GRANTED, "approval:granted")
        return Resolution(approval, True, run_id)

    if waiting is not None:
        await move(waiting.id, TaskEvent.APPROVAL_DENIED, "approval:denied")
    for t in await store.get_tasks(run_id):
        if t.status not in TASK_TERMINAL and t.id != approval.subject:
            await move(t.id, TaskEvent.CANCEL, "run_failed:approval_denied")
    failed = False
    if run.status is RunStatus.WAITING_APPROVAL:
        final = transition_run(run, RunEvent.APPROVAL_DENIED, at=clock())
        await store.append(
            "run.state_changed",
            ev.RunStateChangedPayload(
                from_status=run.status, to_status=final.status, event=RunEvent.APPROVAL_DENIED
            ),
            run_id=run_id,
            ts=clock(),
        )
        await store.append(
            "run.failed",
            ev.RunFailedPayload(
                failure=FailureClass.HUMAN_REJECTION,
                reason=reason or f"{approval.action} denied",
            ),
            run_id=run_id,
            ts=clock(),
        )
        failed = True
    return Resolution(approval, False, run_id, run_failed=failed)
