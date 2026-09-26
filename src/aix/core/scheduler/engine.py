"""Task scheduler (PLAYBOOK §14.1): ready set, parallelism, dependency blocking, cancellation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import anyio

from aix.domain.enums import TaskStatus
from aix.domain.state import TASK_TERMINAL, TaskEvent, tasks_to_block
from aix.domain.tasks import Task


class TaskDriver(Protocol):
    """What the scheduler needs from the orchestrator. The driver owns task state."""

    def tasks(self) -> Sequence[Task]:
        """Current state of every task, in any order."""
        ...

    async def apply(self, task_id: str, event: TaskEvent, *reasons: str) -> Task:
        """Apply a task transition (recording it) and return the updated task."""
        ...

    async def run_task(self, task: Task) -> None:
        """Drive a ``ready`` task to a terminal state through :meth:`apply`.

        Contract: never raises for task-level failures (they end as ``failed``); a cancelled
        attempt ends as ``cancelled``.
        """
        ...

    async def cancel_running(self) -> None:
        """Ask every live attempt to stop; their ``run_task`` calls then return promptly."""
        ...


class Scheduler:
    """Runs a task graph with up to ``max_parallel`` concurrent tasks."""

    def __init__(self, tasks: Sequence[Task], driver: TaskDriver, *, max_parallel: int) -> None:
        if max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        self._initial = [t.id for t in tasks]
        self._driver = driver
        self._max_parallel = max_parallel
        self._running: set[str] = set()
        self._changed = anyio.Event()

    @property
    def _order(self) -> list[str]:
        """Task ids in stable order; tasks the driver adds later (``split_task``) follow."""
        known = [t.id for t in self._driver.tasks()]
        return [*self._initial, *(i for i in known if i not in set(self._initial))]

    def _current(self) -> list[Task]:
        by_id = {t.id: t for t in self._driver.tasks()}
        return [by_id[i] for i in self._order]

    @staticmethod
    def _is_ready(task: Task, by_id: dict[str, Task]) -> bool:
        return task.status is TaskStatus.CREATED and all(
            by_id[d].status is TaskStatus.COMPLETED for d in task.depends_on
        )

    async def _worker(self, task: Task) -> None:
        try:
            await self._driver.run_task(task)
        finally:
            self._running.discard(task.id)
            self._changed.set()

    async def run(self, cancel: anyio.Event) -> list[Task]:
        """Run to completion or cancellation and return the final tasks in input order.

        Contract: a task starts only after all its dependencies are ``completed``; at most
        ``max_parallel`` run at once; tasks whose dependency failed, was cancelled or was blocked
        become ``blocked`` (``dependency_failed``) and never start. On ``cancel``: no new task
        starts, ``driver.cancel_running`` is called once, every non-terminal task ends
        ``cancelled`` and the call returns after running workers finish.
        """
        cancelling = False
        async with anyio.create_task_group() as tg:

            async def watch_cancel() -> None:
                await cancel.wait()
                self._changed.set()

            tg.start_soon(watch_cancel)
            while True:
                self._changed = anyio.Event()
                waiter = self._changed
                if cancel.is_set() and not cancelling:
                    cancelling = True
                    await self._driver.cancel_running()
                if not cancelling:
                    for task_id in tasks_to_block(self._current()):
                        await self._driver.apply(
                            task_id, TaskEvent.DEPENDENCY_FAILED, "dependency_failed"
                        )
                    by_id = {t.id: t for t in self._current()}
                    for task_id in self._order:
                        if len(self._running) >= self._max_parallel:
                            break
                        task = by_id[task_id]
                        if task_id in self._running or not self._is_ready(task, by_id):
                            continue
                        ready = await self._driver.apply(task_id, TaskEvent.DEPS_SATISFIED)
                        self._running.add(task_id)
                        tg.start_soon(self._worker, ready)
                if not self._running:
                    break
                await waiter.wait()
            tg.cancel_scope.cancel()  # stops the cancel watcher
        if cancelling:
            for task in self._current():
                if task.status not in TASK_TERMINAL:
                    await self._driver.apply(task.id, TaskEvent.CANCEL, "run_cancelled")
        return self._current()
