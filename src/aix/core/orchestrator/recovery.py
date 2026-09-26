"""Crash recovery for an orchestrator that died mid-run (PLAYBOOK §8.2, ADR-0026)."""

from __future__ import annotations

import contextlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from aix.core.orchestrator.recorder import RunRecorder
from aix.core.workspace.manager import Workspace, WorkspaceManager
from aix.domain.enums import AttemptStatus, FailureClass, RetryMutation, TaskStatus
from aix.domain.execution import ExecutionResult
from aix.domain.state import TaskEvent, transition_task
from aix.store import events as ev

T = TaskStatus
E = TaskEvent

INTERRUPTED_NOTE = (
    "Your previous attempt was interrupted before it finished (the orchestrator stopped). Its "
    "workspace was discarded and none of its changes were kept. Start again from the current "
    "run branch."
)

# Events that lead a stranded task back to ``ready`` (all edges exist in the §7 task table).
_PATH_TO_READY: dict[TaskStatus, tuple[TaskEvent, ...]] = {
    T.ASSIGNED: (E.ATTEMPT_FAILED, E.RETRY),
    T.RUNNING: (E.ATTEMPT_FAILED, E.RETRY),
    T.VERIFYING: (E.VERIFIED, E.RETRY),
    T.DECIDING: (E.RETRY,),
    T.ACCEPTED: (E.INTEGRATE, E.MERGE_CONFLICT),
    T.INTEGRATING: (E.MERGE_CONFLICT,),
}


@dataclass
class Recovery:
    """What recovery changed, for the caller and for tests."""

    failed_attempts: list[str] = field(default_factory=list[str])
    requeued: list[str] = field(default_factory=list[str])
    completed: list[str] = field(default_factory=list[str])
    notes: dict[str, str] = field(default_factory=dict[str, str])
    """task id -> control-plane note for the next attempt's prompt."""


def pid_file(root: Path, run_id: str) -> Path:
    return root / ".aix" / "runs" / run_id / "orchestrator.pid"


def write_pid(root: Path, run_id: str) -> None:
    path = pid_file(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="utf-8")


def clear_pid(root: Path, run_id: str) -> None:
    with contextlib.suppress(OSError):
        pid_file(root, run_id).unlink()


def orchestrator_alive(root: Path, run_id: str) -> int | None:
    """Pid of a live orchestrator for ``run_id`` (other than this process), else ``None``.

    A pid file left by a crash names a dead (or recycled) pid; a recycled pid is treated as alive,
    the safe side: the user can delete ``.aix/runs/<run>/orchestrator.pid`` to force a resume.
    """
    try:
        pid = int(pid_file(root, run_id).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    if pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid
    return pid


async def recover_interrupted_run(rec: RunRecorder, wm: WorkspaceManager) -> Recovery:
    """Repair the recorded state of a run whose orchestrator died.

    Contract: every attempt still ``created``/``running`` is finished as ``failed`` with
    ``INTERRUPTED`` and its worktree removed (the attempt branch is kept). A task stranded in
    ``assigned``/``running``/``verifying``/``deciding``/``accepted``/``integrating`` goes back to
    ``ready`` through legal transitions, except one whose change was already merged into the run
    branch, which is completed. Terminal, ``waiting_approval`` and untouched tasks are left alone.
    Nothing here trusts the agent: an unfinished attempt has no evidence and is redone.
    """
    store, run_id = rec.store, rec.run.id
    merged = {e.attempt_id for e in await store.events(run_id=run_id, types=["workspace.merged"])}
    out = Recovery()
    for task in await store.get_tasks(run_id):
        attempts = await store.get_attempts(task.id)
        stranded = task.status in _PATH_TO_READY
        for a in attempts:
            if a.status not in (AttemptStatus.CREATED, AttemptStatus.RUNNING):
                continue
            result = ExecutionResult(
                attempt_id=a.id,
                exit_code=None,
                status="failed",
                failure=FailureClass.INTERRUPTED,
                duration_ms=0,
                stream_path=wm.root / ".aix" / "runs" / run_id / f"{a.id}.stream.jsonl",
            )
            await rec.emit(
                "attempt.finished",
                ev.AttemptFinishedPayload(status=AttemptStatus.FAILED, result=result),
                task_id=task.id,
                attempt_id=a.id,
            )
            out.failed_attempts.append(a.id)
            await wm.remove_workspace(Workspace(a.workspace, f"aix/att/{a.id}", a.base_commit))
            await rec.emit(
                "workspace.removed",
                ev.WorkspaceRemovedPayload(path=a.workspace),
                task_id=task.id,
                attempt_id=a.id,
            )
        if not stranded:
            continue
        current = task
        last = attempts[-1] if attempts else None
        if task.status in (T.ACCEPTED, T.INTEGRATING) and last is not None and last.id in merged:
            path: tuple[TaskEvent, ...] = (
                (E.INTEGRATE, E.INTEGRATED) if task.status is T.ACCEPTED else (E.INTEGRATED,)
            )
            out.completed.append(task.id)
        else:
            path = _PATH_TO_READY[task.status]
            out.requeued.append(task.id)
            out.notes[task.id] = INTERRUPTED_NOTE
        for event in path:
            new = transition_task(current, event)
            await rec.emit(
                "task.state_changed",
                ev.TaskStateChangedPayload(
                    from_status=current.status,
                    to_status=new.status,
                    event=event,
                    reason_codes=[f"failure:{FailureClass.INTERRUPTED.value}", "recovery"],
                ),
                task_id=task.id,
            )
            current = new
    return out


INTERRUPTED_MUTATION = RetryMutation.SAME_AGENT_NEW_CONTEXT
