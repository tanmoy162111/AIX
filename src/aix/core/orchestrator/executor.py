"""Multi-task run execution (PLAYBOOK §14): plan, route, schedule, attempt, integrate, finalize.

Until verification (M4) and the Decision Service (M5) exist, an attempt is accepted when the agent
finished, stayed inside its file scope and, for write tasks, changed something. That rule is
``STUB_VERIFICATION`` and is removed in M4.11. The agent's claim is stored, never used as a signal
(§10.4).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import anyio

from aix.agents.protocol import AgentAdapter, AgentHandle, AgentPermissions, AgentRequest
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
from aix.core.orchestrator.attempt import (
    conflict_files,
    execute_agent,
    model_override,
    persist_artifacts,
    render_prompt,
    timeout_override,
)
from aix.core.orchestrator.plan import PlanRunRequest, choose, plan_into
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.router.rules import RoutingContext, route
from aix.core.scheduler.engine import Scheduler
from aix.core.workspace.manager import WorkspaceManager
from aix.core.workspace.scope import scope_violations
from aix.domain.agents import AgentSpec
from aix.domain.enums import (
    AttemptStatus,
    FailureClass,
    RetryMutation,
    RunStatus,
    TaskStatus,
)
from aix.domain.errors import AixError, MergeConflict, NoEligibleAgent, classify
from aix.domain.execution import Attempt, ExecutionResult, ToolCallRecord, Usage
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import RunEvent, TaskEvent, transition_task
from aix.domain.tasks import Task, TaskGraph
from aix.skills.registry import SkillRegistry
from aix.store import events as ev
from aix.store.db import EventStore

STUB_VERIFICATION: Final = True
"""M3 stand-in for verification + decisions (see module docstring). Removed in M4.11."""
CANCEL_GRACE_S: Final = 10.0


def cancel_marker(project_root: Path, run_id: str) -> Path:
    """File whose existence asks the live orchestrator of ``run_id`` to cancel (``aix cancel``)."""
    return project_root / ".aix" / "runs" / run_id / "CANCEL"


@dataclass(frozen=True)
class RunRequest:
    project_root: Path
    goal: str
    skill: str | None = None
    planner_agent: str | None = None
    allow_dirty: bool = False
    keep_worktrees: bool = False
    max_parallel: int | None = None
    """Overrides ``execution.max_parallel``."""


@dataclass(frozen=True)
class TaskSummary:
    task_id: str
    title: str
    type: str
    status: TaskStatus
    agent_id: str | None
    attempts: int
    failure: FailureClass | None


@dataclass(frozen=True)
class RunOutcome:
    run_id: str
    status: RunStatus
    branch: str
    planner: str
    tasks: list[TaskSummary]
    failure: FailureClass | None
    usage: Usage
    duration_ms: int
    warnings: list[str] = field(default_factory=list[str])

    @property
    def exit_code(self) -> int:
        """PLAYBOOK §23.2: 0 success, 1 run failed, 4 cancelled."""
        return {RunStatus.COMPLETED: 0, RunStatus.CANCELLED: 4}.get(self.status, 1)


def _utc() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Live:
    adapter: AgentAdapter
    handle: AgentHandle


@dataclass(frozen=True)
class _End:
    """How one attempt ended."""

    kind: Literal["completed", "failed", "cancelled", "conflict"]
    failure: FailureClass | None = None
    files: list[str] = field(default_factory=list[str])


@dataclass
class _Book:
    """Per-task bookkeeping the outcome needs."""

    agent_id: str | None = None
    attempts: int = 0
    failure: FailureClass | None = None
    usage: Usage = field(default_factory=Usage)


class _Driver:
    """Owns task state and runs one task's attempts; the scheduler calls into it."""

    def __init__(
        self,
        rec: RunRecorder,
        wm: WorkspaceManager,
        graph: TaskGraph,
        specs: Sequence[AgentSpec],
        *,
        registry: AdapterRegistry,
        config: AixConfig,
        keep_worktrees: bool,
    ) -> None:
        self._rec = rec
        self._wm = wm
        self._specs = specs
        self._registry = registry
        self._config = config
        self._keep = keep_worktrees
        self._tasks = {t.id: t for t in graph.tasks}
        self._live: dict[str, _Live] = {}
        self._active = 0
        self._cancelled = False
        self._authors: dict[str, str] = {}
        self.book: dict[str, _Book] = {t.id: _Book() for t in graph.tasks}

    # ---- TaskDriver protocol --------------------------------------------------------------

    def tasks(self) -> list[Task]:
        return list(self._tasks.values())

    async def apply(self, task_id: str, event: TaskEvent, *reasons: str) -> Task:
        task = self._tasks[task_id]
        new = transition_task(task, event)
        await self._rec.emit(
            "task.state_changed",
            ev.TaskStateChangedPayload(
                from_status=task.status,
                to_status=new.status,
                event=event,
                reason_codes=list(reasons),
            ),
            task_id=task_id,
        )
        self._tasks[task_id] = new
        return new

    async def cancel_running(self) -> None:
        """Flag cancellation and stop every live attempt, including ones still starting up."""
        self._cancelled = True
        cancelled: set[str] = set()
        with anyio.CancelScope(shield=True):
            while self._active > 0:
                for attempt_id, live in list(self._live.items()):
                    if attempt_id not in cancelled:
                        cancelled.add(attempt_id)
                        await live.adapter.cancel(live.handle, CANCEL_GRACE_S)
                await anyio.sleep(0.05)

    async def run_task(self, task: Task) -> None:
        """Route and run attempts until the task is terminal (or the run is cancelled)."""
        failed_agents: set[str] = set()
        notes: list[str] = []
        mutation: RetryMutation | None = None
        number = 0
        while True:
            task = self._tasks[task.id]
            if self._cancelled:
                return  # the scheduler cancels whatever is still non-terminal
            try:
                decision = route(
                    RoutingContext(
                        task=task,
                        agents=self._specs,
                        failed_agents=frozenset(failed_agents),
                        authored_by=frozenset(self._authors_for(task)),
                        config=self._config.routing,
                    )
                )
            except NoEligibleAgent as exc:
                self.book[task.id].failure = FailureClass.NO_ELIGIBLE_AGENT
                await self._rec.emit(
                    "agent.failed",
                    ev.AgentFailedPayload(
                        failure=FailureClass.NO_ELIGIBLE_AGENT, message=str(exc)[:500]
                    ),
                    task_id=task.id,
                )
                await self.apply(task.id, TaskEvent.NO_ELIGIBLE_AGENT, "router:no_eligible_agent")
                return
            agent_id = decision.primary
            await self._rec.emit(
                "agent.selected",
                ev.AgentSelectedPayload(
                    agent_id=agent_id,
                    model=model_override(self._config, agent_id),
                    fallbacks=decision.fallbacks,
                    scores=decision.scores,
                    reason_codes=decision.reason_codes,
                ),
                task_id=task.id,
            )
            await self.apply(task.id, TaskEvent.ASSIGN)
            number += 1
            book = self.book[task.id]
            book.agent_id, book.attempts = agent_id, number
            end = await self._attempt(task, agent_id, number, mutation, notes)
            if end.kind == "conflict":
                mutation = RetryMutation.REBASE_AND_RETRY
                notes.append(
                    "Your previous attempt could not be merged: it conflicted with changes already "
                    f"merged into the run branch in: {', '.join(end.files) or 'unknown files'}. "
                    "This workspace starts from the updated branch; redo the work on top of it."
                )
                continue
            if end.kind == "failed":
                book.failure = end.failure
                if end.failure not in (None, FailureClass.AGENT_NO_CHANGES):
                    failed_agents.add(agent_id)
            return

    def _authors_for(self, task: Task) -> set[str]:
        """Agents that authored write tasks this task (transitively) depends on."""
        seen: set[str] = set()
        stack = list(task.depends_on)
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            stack.extend(self._tasks[dep].depends_on)
        return {self._authors[d] for d in seen if d in self._authors}

    # ---- one attempt ----------------------------------------------------------------------

    async def _attempt(
        self,
        task: Task,
        agent_id: str,
        number: int,
        mutation: RetryMutation | None,
        notes: Sequence[str],
    ) -> _End:
        rec, wm, config = self._rec, self._wm, self._config
        adapter = self._registry.get(agent_id)
        attempt_id = new_id(IdPrefix.ATTEMPT)
        run_id = rec.run.id
        t0 = time.monotonic()
        ws = None
        failure: FailureClass | None = None
        end = _End("failed")
        self._active += 1
        try:
            ws = await wm.create_attempt_workspace(run_id, attempt_id)
            await rec.emit(
                "workspace.created",
                ev.WorkspaceCreatedPayload(
                    path=ws.path, branch=ws.branch, base_commit=ws.base_commit
                ),
                task_id=task.id,
                attempt_id=attempt_id,
            )
            attempt = Attempt(
                id=attempt_id,
                task_id=task.id,
                number=number,
                agent_id=agent_id,
                model=model_override(config, agent_id),
                mutation=mutation,
                workspace=ws.path,
                base_commit=ws.base_commit,
            )
            await rec.emit(
                "attempt.created",
                ev.AttemptCreatedPayload(attempt=attempt),
                task_id=task.id,
                attempt_id=attempt_id,
            )
            if self._cancelled:
                await self.apply(task.id, TaskEvent.CANCEL, "attempt:cancelled_before_start")
                return _End("cancelled")
            await self.apply(task.id, TaskEvent.START)

            stream_path = wm.root / ".aix" / "runs" / run_id / f"{attempt_id}.stream.jsonl"
            write_scope = [] if task.file_scope == ["**"] else list(task.file_scope)
            agent_req = AgentRequest(
                attempt_id=attempt_id,
                workspace=ws.path,
                prompt=render_prompt(task, notes),
                model=attempt.model,
                timeout_s=timeout_override(config, agent_id) or config.execution.attempt_timeout_s,
                permissions=AgentPermissions(
                    read_only=not task.file_scope, write_scope=write_scope
                ),
                stream_path=stream_path,
            )
            tool_calls: list[ToolCallRecord] = []
            normalized: list[str] = []

            def register(handle: AgentHandle) -> None:
                self._live[attempt_id] = _Live(adapter, handle)

            outcome = await execute_agent(
                adapter, agent_req, rec.emit, task.id, attempt_id, tool_calls, normalized, register
            )
            self._live.pop(attempt_id, None)
            self.book[task.id].usage = _add_usage(self.book[task.id].usage, outcome.usage)
            cancelled = outcome.status == "cancelled"
            if outcome.status in ("failed", "timeout"):
                await rec.emit(
                    "agent.failed",
                    ev.AgentFailedPayload(
                        failure=outcome.failure or FailureClass.AGENT_FAILURE,
                        message=outcome.stderr_tail[-500:],
                    ),
                    task_id=task.id,
                    attempt_id=attempt_id,
                )
            capture = await wm.capture_diff(ws)
            await persist_artifacts(stream_path, normalized, capture)

            if cancelled:
                failure = None
            elif outcome.status != "completed":
                failure = outcome.failure or FailureClass.AGENT_FAILURE
            else:
                violations = scope_violations(
                    capture.summary.paths, task.file_scope, read_only=not task.file_scope
                )
                for path in violations:
                    await rec.emit(
                        "policy.violation",
                        ev.PolicyViolationPayload(
                            kind="scope",
                            detail="path changed outside the task's file scope",
                            path=path,
                        ),
                        task_id=task.id,
                        attempt_id=attempt_id,
                    )
                if violations:
                    failure = FailureClass.SCOPE_VIOLATION
                elif task.file_scope and capture.summary.files_changed == 0:
                    failure = FailureClass.AGENT_NO_CHANGES

            result = ExecutionResult(
                attempt_id=attempt_id,
                exit_code=outcome.exit_code,
                status=outcome.status,
                failure=failure,
                claim=outcome.claim,
                diff=capture.summary,
                tool_calls=tool_calls,
                usage=outcome.usage,
                duration_ms=int((time.monotonic() - t0) * 1000),
                stream_path=stream_path,
            )
            attempt_status = (
                AttemptStatus.CANCELLED
                if cancelled
                else AttemptStatus.FAILED
                if failure
                else AttemptStatus.COMPLETED
            )
            await rec.emit(
                "attempt.finished",
                ev.AttemptFinishedPayload(status=attempt_status, result=result),
                task_id=task.id,
                attempt_id=attempt_id,
            )

            if cancelled:
                await self.apply(task.id, TaskEvent.CANCEL, "attempt:cancelled")
                return _End("cancelled")
            if outcome.status != "completed":
                await self.apply(
                    task.id, TaskEvent.ATTEMPT_FAILED, f"failure:{failure.value if failure else ''}"
                )
                await self.apply(task.id, TaskEvent.REJECT, "m3:no_retry")
                return _End("failed", failure)
            await self.apply(task.id, TaskEvent.EXECUTION_FINISHED)
            await self.apply(task.id, TaskEvent.VERIFIED, "m3:stub_verification")
            if failure is not None:
                await self.apply(task.id, TaskEvent.REJECT, f"failure:{failure.value}")
                return _End("failed", failure)
            await self.apply(task.id, TaskEvent.ACCEPT, "m3:in_scope")
            await self.apply(task.id, TaskEvent.INTEGRATE)
            end = await self._integrate(task, ws, attempt_id, number)
            return end
        except AixError as exc:
            failure = failure or exc.failure_class
            self.book[task.id].failure = failure
            current = self._tasks[task.id].status
            if current in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
                await self.apply(task.id, TaskEvent.ATTEMPT_FAILED, f"failure:{failure.value}")
                current = TaskStatus.DECIDING
            if current is TaskStatus.DECIDING:
                await self.apply(task.id, TaskEvent.REJECT, f"failure:{failure.value}")
            return _End("failed", failure)
        except Exception as exc:  # unexpected bug: keep the run consistent, then report it
            failure = classify(exc)
            self.book[task.id].failure = failure
            current = self._tasks[task.id].status
            if current in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
                await self.apply(task.id, TaskEvent.ATTEMPT_FAILED, f"failure:{failure.value}")
                await self.apply(task.id, TaskEvent.REJECT, f"failure:{failure.value}")
            return _End("failed", failure)
        finally:
            self._active -= 1
            self._live.pop(attempt_id, None)
            if ws is not None and not self._keep:
                with anyio.CancelScope(shield=True):
                    await wm.remove_workspace(ws)
                    await rec.emit(
                        "workspace.removed",
                        ev.WorkspaceRemovedPayload(path=ws.path),
                        task_id=task.id,
                        attempt_id=attempt_id,
                    )

    async def _integrate(self, task: Task, ws: object, attempt_id: str, number: int) -> _End:
        """Merge an accepted attempt into the run branch (serialized by the workspace manager)."""
        from aix.core.workspace.manager import Workspace

        assert isinstance(ws, Workspace)
        rec, wm = self._rec, self._wm
        if not task.file_scope:  # read-only: nothing to merge
            await self.apply(task.id, TaskEvent.INTEGRATED, "read_only")
            return _End("completed")
        message = f"aix: {task.title} [{task.id}/{attempt_id}]"
        try:
            await wm.commit_attempt(ws, message)
            merge_sha = await wm.merge_attempt(rec.run.id, ws, message)
        except MergeConflict as exc:
            files = conflict_files(exc)
            await rec.emit(
                "workspace.conflict",
                ev.WorkspaceConflictPayload(branch=ws.branch, files=files),
                task_id=task.id,
                attempt_id=attempt_id,
            )
            if number < task.max_attempts:
                await self.apply(task.id, TaskEvent.MERGE_CONFLICT, "retry:rebase_and_retry")
                return _End("conflict", FailureClass.MERGE_CONFLICT, files)
            await self.apply(task.id, TaskEvent.STOP, "retry:exhausted")
            return _End("failed", FailureClass.MERGE_CONFLICT, files)
        await rec.emit(
            "workspace.merged",
            ev.WorkspaceMergedPayload(
                branch=ws.branch, into=f"aix/run/{rec.run.id}", commit=merge_sha
            ),
            task_id=task.id,
            attempt_id=attempt_id,
        )
        await self.apply(task.id, TaskEvent.INTEGRATED)
        agent = self.book[task.id].agent_id
        if agent is not None:
            self._authors[task.id] = agent
        return _End("completed")


def _add_usage(a: Usage, b: Usage) -> Usage:
    def add(x: float | None, y: float | None) -> float | None:
        return None if x is None and y is None else (x or 0.0) + (y or 0.0)

    def addi(x: int | None, y: int | None) -> int | None:
        return None if x is None and y is None else (x or 0) + (y or 0)

    return Usage(
        input_tokens=addi(a.input_tokens, b.input_tokens),
        output_tokens=addi(a.output_tokens, b.output_tokens),
        cost_usd=add(a.cost_usd, b.cost_usd),
        estimated=a.estimated or b.estimated,
    )


async def execute_graph(
    rec: RunRecorder,
    wm: WorkspaceManager,
    graph: TaskGraph,
    *,
    registry: AdapterRegistry,
    config: AixConfig,
    planner: str = "template",
    warnings: Sequence[str] = (),
    keep_worktrees: bool = False,
    max_parallel: int | None = None,
    cancel: anyio.Event | None = None,
) -> RunOutcome:
    """Execute a planned run (``rec.run.status == planned``) to a terminal status.

    Contract: tasks run through the scheduler with ``max_parallel`` concurrency, each attempt in
    its own worktree; accepted write attempts are merged into ``aix/run/<id>`` one at a time; a
    merge conflict retries the task from the new branch head (up to ``max_attempts``). A failed
    task blocks its dependents. ``cancel`` (or the ``aix cancel`` marker file) stops live agents,
    cancels every non-terminal task and ends the run ``cancelled``. The user's checked-out branch
    is never touched.
    """
    t0 = time.monotonic()
    cancel = cancel or anyio.Event()
    run_id = rec.run.id
    specs = await registry.probe_all()
    driver = _Driver(
        rec, wm, graph, specs, registry=registry, config=config, keep_worktrees=keep_worktrees
    )
    await rec.run_to(RunEvent.START_EXECUTION)
    marker = cancel_marker(wm.root, run_id)

    async def watch_marker() -> None:
        while not cancel.is_set():
            if await anyio.Path(marker).exists():
                cancel.set()
                return
            await anyio.sleep(0.25)

    scheduler = Scheduler(
        graph.tasks, driver, max_parallel=max_parallel or config.execution.max_parallel
    )
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(watch_marker)
            await scheduler.run(cancel)
            tg.cancel_scope.cancel()
    except BaseException:
        with anyio.CancelScope(shield=True):
            await driver.cancel_running()
        raise

    final = driver.tasks()
    branch = f"aix/run/{run_id}"
    await rec.run_to(RunEvent.ALL_TASKS_TERMINAL)
    failure: FailureClass | None = None
    if all(t.status is TaskStatus.COMPLETED for t in final):
        await rec.run_to(RunEvent.COMPLETE)
        await rec.emit("run.completed", ev.RunCompletedPayload(summary=f"merged into {branch}"))
    elif cancel.is_set():
        await rec.run_to(RunEvent.CANCEL)
        await rec.emit("run.cancelled", ev.RunCancelledPayload(reason="cancelled by user"))
    else:
        failure = next(
            (
                driver.book[t.id].failure
                for t in final
                if t.status is TaskStatus.FAILED and driver.book[t.id].failure
            ),
            FailureClass.AGENT_FAILURE,
        )
        await rec.run_to(RunEvent.FAIL)
        failed = [t.title for t in final if t.status is TaskStatus.FAILED]
        await rec.emit(
            "run.failed",
            ev.RunFailedPayload(failure=failure, reason=f"failed tasks: {', '.join(failed)}"),
        )

    usage = Usage()
    for b in driver.book.values():
        usage = _add_usage(usage, b.usage)
    summaries = [
        TaskSummary(
            task_id=t.id,
            title=t.title,
            type=t.type.value,
            status=t.status,
            agent_id=driver.book[t.id].agent_id,
            attempts=driver.book[t.id].attempts,
            failure=driver.book[t.id].failure,
        )
        for t in final
    ]
    return RunOutcome(
        run_id=run_id,
        status=rec.run.status,
        branch=branch,
        planner=planner,
        tasks=summaries,
        failure=failure,
        usage=usage,
        duration_ms=int((time.monotonic() - t0) * 1000),
        warnings=list(warnings),
    )


async def execute_run(
    req: RunRequest,
    *,
    registry: AdapterRegistry,
    store: EventStore,
    config: AixConfig,
    skills: SkillRegistry | None = None,
    clock: Callable[[], datetime] = _utc,
    cancel: anyio.Event | None = None,
) -> RunOutcome:
    """Plan a goal and execute the plan (``aix run "<goal>"``).

    Raises:
        ConfigError: unknown skill or planner agent.
        ToolFailure: not a git repository, or a dirty tree without ``allow_dirty`` (nothing is
            recorded).
    """

    skills = skills or SkillRegistry.builtin()
    wm = WorkspaceManager(req.project_root)
    plan_req = PlanRunRequest(
        project_root=req.project_root,
        goal=req.goal,
        skill=req.skill,
        planner_agent=req.planner_agent,
        allow_dirty=req.allow_dirty,
    )
    setup = await choose(plan_req, registry, config, skills)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id, allow_dirty=req.allow_dirty)  # preflights too
    rec = RunRecorder(store, new_run(run_id, wm.root, req.goal, config, clock()), clock)
    await rec.start()
    planned = await plan_into(
        rec, plan_req, setup, wm=wm, registry=registry, config=config, skills=skills
    )
    return await execute_graph(
        rec,
        wm,
        planned.graph,
        registry=registry,
        config=config,
        planner=planned.planner,
        warnings=planned.warnings,
        keep_worktrees=req.keep_worktrees,
        max_parallel=req.max_parallel,
        cancel=cancel,
    )
