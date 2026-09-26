"""Read-only run snapshot for ``aix status`` (and the API later): tasks, agents, attempts."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from aix.domain.enums import RunStatus, TaskStatus
from aix.domain.errors import ConfigError
from aix.domain.state import RUN_TERMINAL
from aix.store.db import EventStore


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    number: int
    type: str
    title: str
    status: TaskStatus
    agent: str | None
    attempts: int
    depends_on: list[int]
    failure: str | None


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    goal: str
    status: RunStatus
    planner: str
    branch: str
    tasks: list[TaskRow] = field(default_factory=list[TaskRow])

    @property
    def counts(self) -> dict[str, int]:
        """Number of tasks per status value."""
        return dict(Counter(t.status.value for t in self.tasks))

    @property
    def terminal(self) -> bool:
        return self.status in RUN_TERMINAL


async def latest_run_id(store: EventStore) -> str | None:
    """Id of the newest run, or ``None`` when there are none."""
    runs = await store.list_runs()
    return runs[-1].id if runs else None


async def snapshot(store: EventStore, run_id: str) -> RunSnapshot:
    """Build the current view of a run from the projections.

    Raises:
        ConfigError: unknown run.
    """
    from aix.store import events as ev

    run = await store.get_run(run_id)
    if run is None:
        raise ConfigError(f"unknown run {run_id!r}")
    planned = await store.events(run_id=run_id, types=["run.planned"])
    payload = planned[-1].payload if planned else None
    planner = payload.planner if isinstance(payload, ev.RunPlannedPayload) else "-"
    tasks = await store.get_tasks(run_id)
    number = {t.id: i for i, t in enumerate(tasks, start=1)}
    rows: list[TaskRow] = []
    for task in tasks:
        attempts = await store.get_attempts(task.id)
        agent = attempts[-1].agent_id if attempts else None
        failure: str | None = None
        for attempt in reversed(attempts):
            result = await store.get_result(attempt.id)
            if result is not None:
                failure = result.failure.value if result.failure else None
                break
        rows.append(
            TaskRow(
                task_id=task.id,
                number=number[task.id],
                type=task.type.value,
                title=task.title,
                status=task.status,
                agent=agent,
                attempts=len(attempts),
                depends_on=[number[d] for d in task.depends_on],
                failure=failure,
            )
        )
    return RunSnapshot(
        run_id=run.id,
        goal=run.goal,
        status=run.status,
        planner=planner,
        branch=f"aix/run/{run.id}",
        tasks=rows,
    )
