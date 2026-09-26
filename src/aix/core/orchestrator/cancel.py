"""Cancelling a run without a live orchestrator (``aix cancel``, PLAYBOOK §14.1)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from aix.domain.enums import RunStatus
from aix.domain.errors import ConfigError
from aix.domain.state import RUN_TERMINAL, TASK_TERMINAL, RunEvent, TaskEvent, transition_task
from aix.store import events as ev
from aix.store.db import EventStore

TERMINAL_RUN_STATUSES = RUN_TERMINAL
DIRECT_CANCEL_STATUSES = frozenset(
    {RunStatus.CREATED, RunStatus.PLANNED, RunStatus.WAITING_APPROVAL}
)
"""Statuses nobody drives, so the run can be cancelled without asking a live orchestrator."""


def _utc() -> datetime:
    return datetime.now(UTC)


async def force_cancel(
    store: EventStore, run_id: str, *, reason: str, clock: Callable[[], datetime] = _utc
) -> RunStatus:
    """Cancel every non-terminal task and the run itself by appending events.

    Contract: only safe when no orchestrator is live for ``run_id``. Idempotent for terminal runs.

    Raises:
        ConfigError: unknown run.
    """
    run = await store.get_run(run_id)
    if run is None:
        raise ConfigError(f"unknown run {run_id!r}")
    if run.status in TERMINAL_RUN_STATUSES:
        return run.status
    for task in await store.get_tasks(run_id):
        if task.status in TASK_TERMINAL:
            continue
        new = transition_task(task, TaskEvent.CANCEL)
        await store.append(
            "task.state_changed",
            ev.TaskStateChangedPayload(
                from_status=task.status,
                to_status=new.status,
                event=TaskEvent.CANCEL,
                reason_codes=["run_cancelled"],
            ),
            run_id=run_id,
            task_id=task.id,
            ts=clock(),
        )
    from aix.domain.state import transition_run

    status = run.status
    if status is RunStatus.EXECUTING:
        nxt = transition_run(run, RunEvent.ALL_TASKS_TERMINAL, at=clock())
        await store.append(
            "run.state_changed",
            ev.RunStateChangedPayload(
                from_status=status, to_status=nxt.status, event=RunEvent.ALL_TASKS_TERMINAL
            ),
            run_id=run_id,
            ts=clock(),
        )
        run = nxt
    final = transition_run(run, RunEvent.CANCEL, at=clock())
    await store.append(
        "run.state_changed",
        ev.RunStateChangedPayload(
            from_status=run.status, to_status=final.status, event=RunEvent.CANCEL
        ),
        run_id=run_id,
        ts=clock(),
    )
    await store.append(
        "run.cancelled", ev.RunCancelledPayload(reason=reason), run_id=run_id, ts=clock()
    )
    return final.status
