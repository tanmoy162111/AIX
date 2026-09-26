"""Table-driven Run and Task state machines (PLAYBOOK §7).

The tables are the single source of truth: ``transition_task`` / ``transition_run`` look up
``(status, event)`` and either return an updated copy or raise ``IllegalTransition``. Terminal
states (completed, failed, cancelled) have no outgoing edges, so they absorb every event.
Nothing here performs I/O or reads the clock; callers pass ``at`` for terminal run transitions.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from aix.domain.enums import (
    RUN_TERMINAL,
    TASK_TERMINAL,
    RunStatus,
    TaskStatus,
)
from aix.domain.errors import IllegalTransition
from aix.domain.ids import TaskId
from aix.domain.runs import Run
from aix.domain.tasks import Task


class TaskEvent(StrEnum):
    """Inputs to the task state machine."""

    DEPS_SATISFIED = "deps_satisfied"
    ASSIGN = "assign"
    START = "start"
    EXECUTION_FINISHED = "execution_finished"
    ATTEMPT_FAILED = "attempt_failed"
    VERIFIED = "verified"
    ACCEPT = "accept"
    RETRY = "retry"
    SWITCH_AGENT = "switch_agent"
    ESCALATE = "escalate"
    ASK_HUMAN = "ask_human"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    REJECT = "reject"
    STOP = "stop"
    INTEGRATE = "integrate"
    INTEGRATED = "integrated"
    MERGE_CONFLICT = "merge_conflict"
    NO_ELIGIBLE_AGENT = "no_eligible_agent"
    DEPENDENCY_FAILED = "dependency_failed"
    CANCEL = "cancel"


class RunEvent(StrEnum):
    """Inputs to the run state machine."""

    START_PLANNING = "start_planning"
    PLANNED = "planned"
    START_EXECUTION = "start_execution"
    AWAIT_APPROVAL = "await_approval"
    APPROVAL_GRANTED = "approval_granted"
    APPROVAL_DENIED = "approval_denied"
    ALL_TASKS_TERMINAL = "all_tasks_terminal"
    COMPLETE = "complete"
    FAIL = "fail"
    CANCEL = "cancel"


_T = TaskStatus
_E = TaskEvent


def _build_task_table() -> dict[tuple[TaskStatus, TaskEvent], TaskStatus]:
    table: dict[tuple[TaskStatus, TaskEvent], TaskStatus] = {
        # main line (§7.2)
        (_T.CREATED, _E.DEPS_SATISFIED): _T.READY,
        (_T.READY, _E.ASSIGN): _T.ASSIGNED,
        (_T.ASSIGNED, _E.START): _T.RUNNING,
        (_T.RUNNING, _E.EXECUTION_FINISHED): _T.VERIFYING,
        (_T.VERIFYING, _E.VERIFIED): _T.DECIDING,
        # decisions
        (_T.DECIDING, _E.ACCEPT): _T.ACCEPTED,
        (_T.DECIDING, _E.RETRY): _T.READY,
        (_T.DECIDING, _E.SWITCH_AGENT): _T.READY,
        (_T.DECIDING, _E.ESCALATE): _T.READY,
        (_T.DECIDING, _E.ASK_HUMAN): _T.WAITING_APPROVAL,
        (_T.DECIDING, _E.REJECT): _T.FAILED,
        (_T.DECIDING, _E.STOP): _T.FAILED,
        (_T.WAITING_APPROVAL, _E.APPROVAL_GRANTED): _T.READY,
        (_T.WAITING_APPROVAL, _E.APPROVAL_DENIED): _T.FAILED,
        # integration
        (_T.ACCEPTED, _E.INTEGRATE): _T.INTEGRATING,
        (_T.INTEGRATING, _E.INTEGRATED): _T.COMPLETED,
        # additions to the spec's diagram, recorded in ADR-0006
        (_T.ASSIGNED, _E.ATTEMPT_FAILED): _T.DECIDING,
        (_T.RUNNING, _E.ATTEMPT_FAILED): _T.DECIDING,
        (_T.INTEGRATING, _E.MERGE_CONFLICT): _T.READY,
        (_T.READY, _E.NO_ELIGIBLE_AGENT): _T.FAILED,
    }
    for status in TaskStatus:
        if status in TASK_TERMINAL:
            continue
        table[(status, _E.CANCEL)] = _T.CANCELLED
        if status is not _T.BLOCKED:
            table[(status, _E.DEPENDENCY_FAILED)] = _T.BLOCKED
    return table


def _build_run_table() -> dict[tuple[RunStatus, RunEvent], RunStatus]:
    r, e = RunStatus, RunEvent
    table: dict[tuple[RunStatus, RunEvent], RunStatus] = {
        (r.CREATED, e.START_PLANNING): r.PLANNING,
        (r.PLANNING, e.PLANNED): r.PLANNED,
        (r.PLANNING, e.FAIL): r.FAILED,
        (r.PLANNED, e.START_EXECUTION): r.EXECUTING,
        (r.PLANNED, e.AWAIT_APPROVAL): r.WAITING_APPROVAL,  # plan_review on high-risk intents
        (r.EXECUTING, e.AWAIT_APPROVAL): r.WAITING_APPROVAL,
        (r.WAITING_APPROVAL, e.APPROVAL_GRANTED): r.EXECUTING,
        (r.WAITING_APPROVAL, e.APPROVAL_DENIED): r.FAILED,
        (r.WAITING_APPROVAL, e.FAIL): r.FAILED,
        (r.EXECUTING, e.ALL_TASKS_TERMINAL): r.FINALIZING,
        (r.EXECUTING, e.FAIL): r.FAILED,  # budget / stop decision
        (r.FINALIZING, e.COMPLETE): r.COMPLETED,
        (r.FINALIZING, e.FAIL): r.FAILED,  # run_completion rejected
    }
    for status in RunStatus:
        if status not in RUN_TERMINAL:
            table[(status, e.CANCEL)] = r.CANCELLED
    return table


TASK_TRANSITIONS: Mapping[tuple[TaskStatus, TaskEvent], TaskStatus] = MappingProxyType(
    _build_task_table()
)
"""``(status, event) -> next status`` for tasks."""

RUN_TRANSITIONS: Mapping[tuple[RunStatus, RunEvent], RunStatus] = MappingProxyType(
    _build_run_table()
)
"""``(status, event) -> next status`` for runs."""


def transition_task(task: Task, event: TaskEvent) -> Task:
    """Return ``task`` moved by ``event``.

    Raises:
        IllegalTransition: if the table has no edge for ``(task.status, event)``. The input task is
            never mutated.
    """
    nxt = TASK_TRANSITIONS.get((task.status, event))
    if nxt is None:
        raise IllegalTransition(
            f"task {task.id}: event {event.value!r} is not allowed in state {task.status.value!r}",
            details={"task_id": task.id, "status": task.status.value, "event": event.value},
        )
    return task.model_copy(update={"status": nxt})


def transition_run(run: Run, event: RunEvent, *, at: datetime) -> Run:
    """Return ``run`` moved by ``event``; entering a terminal state sets ``finished_at`` to ``at``.

    Raises:
        IllegalTransition: if the table has no edge for ``(run.status, event)``.
    """
    nxt = RUN_TRANSITIONS.get((run.status, event))
    if nxt is None:
        raise IllegalTransition(
            f"run {run.id}: event {event.value!r} is not allowed in state {run.status.value!r}",
            details={"run_id": run.id, "status": run.status.value, "event": event.value},
        )
    update: dict[str, object] = {"status": nxt}
    if nxt in RUN_TERMINAL:
        update["finished_at"] = at
    return run.model_copy(update=update)


_DEAD = frozenset({TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED})


def tasks_to_block(tasks: Sequence[Task]) -> list[TaskId]:
    """Ids of tasks that must receive ``DEPENDENCY_FAILED``.

    A live task (not terminal, not already blocked) is selected when any dependency is failed,
    cancelled, blocked, or itself selected, so blocking propagates transitively (§7.2). Completed
    tasks are never selected. The result is in discovery order.
    """
    dead = {t.id for t in tasks if t.status in _DEAD}
    selected: list[TaskId] = []
    changed = True
    while changed:
        changed = False
        for t in tasks:
            if t.status in TASK_TERMINAL or t.status is TaskStatus.BLOCKED or t.id in dead:
                continue
            if any(dep in dead for dep in t.depends_on):
                selected.append(t.id)
                dead.add(t.id)
                changed = True
    return selected
