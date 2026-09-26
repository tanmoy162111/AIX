from __future__ import annotations

import anyio
import pytest

from aix.core.scheduler.engine import Scheduler
from aix.domain.enums import TaskStatus, TaskType
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import TaskEvent, transition_task
from aix.domain.tasks import Task

pytestmark = pytest.mark.anyio

RUN = new_id(IdPrefix.RUN)
E = TaskEvent


def make(title: str, deps: list[Task] | None = None) -> Task:
    return Task(
        id=new_id(IdPrefix.TASK),
        run_id=RUN,
        title=title,
        goal="g",
        type=TaskType.IMPLEMENT,
        depends_on=[d.id for d in deps or []],
    )


class StubDriver:
    """Drives tasks through the happy path (or a scripted failure) and records what happened."""

    def __init__(
        self, tasks: list[Task], *, fail: frozenset[str] = frozenset(), hold: bool = False
    ) -> None:
        self.state = {t.id: t for t in tasks}
        self.by_title = {t.title: t.id for t in tasks}
        self.fail = fail
        self.hold = anyio.Event() if hold else None
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running = 0
        self.peak = 0
        self.cancel_calls = 0
        self.entered = anyio.Event()

    async def apply(self, task_id: str, event: TaskEvent, *reasons: str) -> Task:
        self.state[task_id] = transition_task(self.state[task_id], event)
        return self.state[task_id]

    def tasks(self) -> list[Task]:
        return list(self.state.values())

    async def run_task(self, task: Task) -> None:
        self.started.append(task.title)
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await self.apply(task.id, E.ASSIGN)
            await self.apply(task.id, E.START)
            if self.hold is not None:
                self.entered.set()
                await self.hold.wait()
                await self.apply(task.id, E.CANCEL)
                return
            await anyio.sleep(0.01)
            if task.title in self.fail:
                await self.apply(task.id, E.ATTEMPT_FAILED)
                await self.apply(task.id, E.REJECT)
            else:
                for ev in (E.EXECUTION_FINISHED, E.VERIFIED, E.ACCEPT, E.INTEGRATE, E.INTEGRATED):
                    await self.apply(task.id, ev)
            self.finished.append(task.title)
        finally:
            self.running -= 1

    async def cancel_running(self) -> None:
        self.cancel_calls += 1
        if self.hold is not None:
            self.hold.set()


def status(d: StubDriver, title: str) -> TaskStatus:
    return d.state[d.by_title[title]].status


async def run(tasks: list[Task], driver: StubDriver, max_parallel: int = 3) -> list[Task]:
    cancel = anyio.Event()
    with anyio.fail_after(10):
        return await Scheduler(tasks, driver, max_parallel=max_parallel).run(cancel)


async def test_chain_runs_in_dependency_order() -> None:
    a = make("a")
    b = make("b", [a])
    c = make("c", [b])
    d = StubDriver([c, b, a])  # listing order must not matter
    final = await run([c, b, a], d)
    assert d.started == ["a", "b", "c"]
    assert {t.status for t in final} == {TaskStatus.COMPLETED}


async def test_diamond_runs_middle_tasks_in_parallel() -> None:
    a = make("a")
    b, c = make("b", [a]), make("c", [a])
    z = make("z", [b, c])
    d = StubDriver([a, b, c, z])
    await run([a, b, c, z], d)
    assert d.started[0] == "a" and d.started[-1] == "z" and d.peak == 2


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_max_parallel_is_never_exceeded(limit: int) -> None:
    tasks = [make(f"t{i}") for i in range(6)]
    d = StubDriver(tasks)
    await run(tasks, d, limit)
    assert d.peak == limit and len(d.finished) == 6


async def test_failure_blocks_transitive_dependents_but_not_siblings() -> None:
    a = make("a")
    b, c = make("b", [a]), make("c", [a])
    z = make("z", [b])
    w = make("w", [z])
    d = StubDriver([a, b, c, z, w], fail=frozenset({"b"}))
    await run([a, b, c, z, w], d)
    assert status(d, "b") is TaskStatus.FAILED
    assert status(d, "c") is TaskStatus.COMPLETED
    assert status(d, "z") is TaskStatus.BLOCKED and status(d, "w") is TaskStatus.BLOCKED
    assert "z" not in d.started and "w" not in d.started


async def test_root_failure_blocks_everything_downstream() -> None:
    a = make("a")
    b = make("b", [a])
    d = StubDriver([a, b], fail=frozenset({"a"}))
    await run([a, b], d)
    assert status(d, "b") is TaskStatus.BLOCKED and d.started == ["a"]


async def test_cancellation_cancels_running_pending_and_blocked_tasks() -> None:
    a = make("a")
    b = make("b", [a])
    free = make("free")
    d = StubDriver([a, b, free], hold=True)
    cancel = anyio.Event()
    sched = Scheduler([a, b, free], d, max_parallel=1)

    async with anyio.create_task_group() as tg:
        result: list[list[Task]] = []

        async def go() -> None:
            result.append(await sched.run(cancel))

        tg.start_soon(go)
        with anyio.fail_after(5):
            await d.entered.wait()
        cancel.set()
    (final,) = result
    assert d.cancel_calls == 1
    assert {t.status for t in final} == {TaskStatus.CANCELLED}
    assert d.started == ["a"]  # max_parallel=1: nothing else ever started


async def test_cancel_before_start_cancels_everything_without_running_anything() -> None:
    tasks = [make("a"), make("b")]
    d = StubDriver(tasks)
    cancel = anyio.Event()
    cancel.set()
    final = await Scheduler(tasks, d, max_parallel=2).run(cancel)
    assert d.started == [] and {t.status for t in final} == {TaskStatus.CANCELLED}


async def test_returns_final_states_in_input_order() -> None:
    tasks = [make("a"), make("b")]
    final = await run(tasks, StubDriver(tasks))
    assert [t.title for t in final] == ["a", "b"]
