"""Cancelling a run without a live orchestrator (``aix cancel``, PLAYBOOK §14.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anyio

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


@dataclass(frozen=True)
class CancelResult:
    """Outcome of :func:`request_cancel`."""

    kind: Literal["unknown", "already", "cancelled", "no_response"]
    status: str
    """The run's status after the request (empty for ``unknown``)."""


async def request_cancel(
    store: EventStore,
    project_root: Path,
    run_id: str,
    *,
    wait_s: float = 15.0,
    force: bool = False,
    poll_s: float = 0.2,
) -> CancelResult:
    """Cancel a run, asking its live orchestrator to stop when there is one.

    Runs nobody drives (created, planned, waiting for approval) are cancelled directly. For a run
    in flight a marker file is written and the run is watched for up to ``wait_s`` seconds; with
    ``force`` a silent orchestrator is overridden by :func:`force_cancel`. Terminal runs are left
    alone. Shared by ``aix cancel`` and ``POST /runs/{id}/cancel``.
    """
    from aix.core.orchestrator.executor import cancel_marker

    run = await store.get_run(run_id)
    if run is None:
        return CancelResult("unknown", "")
    if run.status in TERMINAL_RUN_STATUSES:
        return CancelResult("already", run.status.value)
    if run.status in DIRECT_CANCEL_STATUSES:
        status = await force_cancel(store, run_id, reason="cancelled by user")
        return CancelResult("cancelled", status.value)
    marker = cancel_marker(project_root, run_id)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("cancel\n", encoding="utf-8")
    with anyio.move_on_after(wait_s):
        while True:
            current = await store.get_run(run_id)
            if current is not None and current.status in TERMINAL_RUN_STATUSES:
                done = current.status.value
                return CancelResult("cancelled" if done == "cancelled" else "already", done)
            await anyio.sleep(poll_s)
    if force:
        status = await force_cancel(store, run_id, reason="cancelled by user (forced)")
        return CancelResult("cancelled", status.value)
    return CancelResult("no_response", run.status.value)
