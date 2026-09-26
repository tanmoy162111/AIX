"""Single-task orchestration (M2.11): one goal, one agent, one attempt, no retries.

Flow (PLAYBOOK §14.2, simplified until verification/decisions land in M4/M5):
run branch -> attempt worktree -> agent -> control-plane diff -> scope check -> accept/reject ->
commit + merge into the run branch -> cleanup. Every step is an event; state changes go through the
table-driven state machines. The agent's claim is stored but never used as a signal (§10.4).
The simple accept rule ("agent finished, produced a diff, stayed in scope") is a stand-in for the
verification engine and Decision Service and is replaced in M4/M5.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import anyio

from aix.agents.protocol import AgentOutcome, AgentPermissions, AgentRequest
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
from aix.core.workspace.manager import DiffCapture, Workspace, WorkspaceManager
from aix.core.workspace.scope import scope_violations
from aix.domain.enums import (
    AttemptStatus,
    Capability,
    FailureClass,
    RunStatus,
    TaskStatus,
    TaskType,
)
from aix.domain.errors import AixError, MergeConflict, NoEligibleAgent
from aix.domain.execution import Attempt, DiffSummary, ExecutionResult, ToolCallRecord, Usage
from aix.domain.ids import IdPrefix, new_id
from aix.domain.runs import Budget, Run
from aix.domain.state import RunEvent, TaskEvent, transition_run, transition_task
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.store import events as ev
from aix.store.db import EventStore


@dataclass(frozen=True)
class SingleTaskRequest:
    project_root: Path
    goal: str
    agent_id: str
    file_scope: list[str] = field(default_factory=lambda: ["**"])
    allow_dirty: bool = False
    keep_worktrees: bool = False
    model: str | None = None


@dataclass(frozen=True)
class RunResult:
    run_id: str
    status: RunStatus
    branch: str
    task_id: str
    attempt_id: str | None
    agent_id: str
    failure: FailureClass | None
    claim: str | None
    diff: DiffSummary | None
    usage: Usage
    duration_ms: int

    @property
    def exit_code(self) -> int:
        """PLAYBOOK §23.2: 0 success, 1 run failed, 4 cancelled."""
        return {RunStatus.COMPLETED: 0, RunStatus.CANCELLED: 4}.get(self.status, 1)


def _utc() -> datetime:
    return datetime.now(UTC)


async def run_single_task(
    req: SingleTaskRequest,
    *,
    registry: AdapterRegistry,
    store: EventStore,
    config: AixConfig,
    clock: Callable[[], datetime] = _utc,
) -> RunResult:
    """Execute one goal with one agent and record everything.

    Raises:
        ConfigError: unknown agent id.
        NoEligibleAgent: the agent is unavailable or disabled (nothing is recorded).
        ToolFailure: not a git repository, or a dirty tree without ``allow_dirty`` (nothing is
            recorded).
    """
    spec = await registry.probe(req.agent_id)
    if spec.health not in ("ready", "degraded"):
        raise NoEligibleAgent(
            f"agent {req.agent_id!r} is {spec.health}: {spec.health_reason or 'no reason given'}"
        )
    adapter = registry.get(req.agent_id)

    wm = WorkspaceManager(req.project_root)
    run_id = new_id(IdPrefix.RUN)
    branch = await wm.create_run_branch(run_id, allow_dirty=req.allow_dirty)  # preflights too

    async def emit(
        etype: str,
        payload: ev.Payload,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        await store.append(
            etype, payload, run_id=run_id, task_id=task_id, attempt_id=attempt_id, ts=clock()
        )

    budget = Budget(
        max_cost_usd=config.budget.max_cost_usd_per_run,
        max_attempts_total=config.budget.max_attempts_per_run,
        max_wall_seconds=config.budget.max_wall_seconds_per_run,
    )
    run = Run(
        id=run_id,
        project_root=wm.root,
        goal=req.goal,
        budget=budget,
        created_at=clock(),
    )
    t0 = time.monotonic()

    async def run_to(event: RunEvent) -> None:
        nonlocal run
        new = transition_run(run, event, at=clock())
        await emit(
            "run.state_changed",
            ev.RunStateChangedPayload(from_status=run.status, to_status=new.status, event=event),
        )
        run = new

    task = Task(
        id=new_id(IdPrefix.TASK),
        run_id=run_id,
        title=req.goal.strip().splitlines()[0][:80] or "task",
        goal=req.goal,
        type=TaskType.IMPLEMENT,
        required_capabilities=[Capability.IMPLEMENT],
        file_scope=list(req.file_scope),
        verification=VerificationSpec(),
        max_attempts=1,
    )

    async def task_to(event: TaskEvent, *reasons: str) -> None:
        nonlocal task
        new = transition_task(task, event)
        await emit(
            "task.state_changed",
            ev.TaskStateChangedPayload(
                from_status=task.status,
                to_status=new.status,
                event=event,
                reason_codes=list(reasons),
            ),
            task_id=task.id,
        )
        task = new

    # ---- plan (trivial: one task) ---------------------------------------------------------
    await emit("run.created", ev.RunCreatedPayload(run=run))
    await run_to(RunEvent.START_PLANNING)
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=[task])
    intent = Intent(goal=req.goal, kind="coding", risk="low")
    await emit("run.planned", ev.RunPlannedPayload(intent=intent, graph=graph))
    await emit("task.created", ev.TaskCreatedPayload(task=task), task_id=task.id)
    await run_to(RunEvent.PLANNED)
    await run_to(RunEvent.START_EXECUTION)

    # ---- route + assign -------------------------------------------------------------------
    await task_to(TaskEvent.DEPS_SATISFIED)
    await emit(
        "agent.selected",
        ev.AgentSelectedPayload(agent_id=req.agent_id, reason_codes=["static:--agent"]),
        task_id=task.id,
    )
    await task_to(TaskEvent.ASSIGN)

    attempt_id = new_id(IdPrefix.ATTEMPT)
    ws: Workspace | None = None
    outcome: AgentOutcome | None = None
    capture: DiffCapture | None = None
    failure: FailureClass | None = None
    tool_calls: list[ToolCallRecord] = []
    stream_path = wm.root / ".aix" / "runs" / run_id / f"{attempt_id}.stream.jsonl"
    normalized: list[str] = []
    cancelled = False
    final_failure_note: str | None = None

    try:
        ws = await wm.create_attempt_workspace(run_id, attempt_id)
        await emit(
            "workspace.created",
            ev.WorkspaceCreatedPayload(path=ws.path, branch=ws.branch, base_commit=ws.base_commit),
            task_id=task.id,
            attempt_id=attempt_id,
        )
        attempt = Attempt(
            id=attempt_id,
            task_id=task.id,
            number=1,
            agent_id=req.agent_id,
            model=req.model or model_override(config, req.agent_id),
            workspace=ws.path,
            base_commit=ws.base_commit,
        )
        await emit(
            "attempt.created", ev.AttemptCreatedPayload(attempt=attempt),
            task_id=task.id, attempt_id=attempt_id,
        )  # fmt: skip
        await task_to(TaskEvent.START)

        write_scope = [] if task.file_scope == ["**"] else list(task.file_scope)
        agent_req = AgentRequest(
            attempt_id=attempt_id,
            workspace=ws.path,
            prompt=render_prompt(task),
            model=attempt.model,
            timeout_s=timeout_override(config, req.agent_id) or config.execution.attempt_timeout_s,
            permissions=AgentPermissions(read_only=not task.file_scope, write_scope=write_scope),
            stream_path=stream_path,
        )
        outcome = await execute_agent(
            adapter, agent_req, emit, task.id, attempt_id, tool_calls, normalized
        )
        cancelled = outcome.status == "cancelled"
        if outcome.status in ("failed", "timeout"):
            await emit(
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
                await emit(
                    "policy.violation",
                    ev.PolicyViolationPayload(
                        kind="scope", detail="path changed outside the task's file scope", path=path
                    ),
                    task_id=task.id,
                    attempt_id=attempt_id,
                )
            if violations:
                failure = FailureClass.SCOPE_VIOLATION
            elif capture.summary.files_changed == 0:
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
        await emit(
            "attempt.finished",
            ev.AttemptFinishedPayload(status=attempt_status, result=result),
            task_id=task.id,
            attempt_id=attempt_id,
        )

        # ---- decide (stand-in for verification + Decision Service) ---------------------------
        if cancelled:
            await task_to(TaskEvent.CANCEL, "attempt:cancelled")
        elif outcome.status != "completed":
            await task_to(TaskEvent.ATTEMPT_FAILED, f"failure:{failure.value if failure else ''}")
            await task_to(TaskEvent.REJECT, "m2:no_retry")
        else:
            await task_to(TaskEvent.EXECUTION_FINISHED)
            await task_to(TaskEvent.VERIFIED, "m2:no_verification")
            if failure is not None:
                await task_to(TaskEvent.REJECT, f"failure:{failure.value}")
            else:
                await task_to(TaskEvent.ACCEPT, "m2:diff_in_scope")
                await task_to(TaskEvent.INTEGRATE)
                message = f"aix: {task.title} [{task.id}/{attempt_id}]"
                try:
                    await wm.commit_attempt(ws, message)
                    merge_sha = await wm.merge_attempt(run_id, ws, message)
                except MergeConflict as exc:
                    await emit(
                        "workspace.conflict",
                        ev.WorkspaceConflictPayload(
                            branch=ws.branch,
                            files=conflict_files(exc),
                        ),
                        task_id=task.id,
                        attempt_id=attempt_id,
                    )
                    await task_to(TaskEvent.MERGE_CONFLICT)  # single task: cannot happen until M3.9
                    failure = FailureClass.MERGE_CONFLICT
                else:
                    await emit(
                        "workspace.merged",
                        ev.WorkspaceMergedPayload(
                            branch=ws.branch, into=branch.name, commit=merge_sha
                        ),
                        task_id=task.id,
                        attempt_id=attempt_id,
                    )
                    await task_to(TaskEvent.INTEGRATED)
    except AixError as exc:
        failure = failure or exc.failure_class
        final_failure_note = str(exc)
        if task.status in (TaskStatus.ASSIGNED, TaskStatus.RUNNING):
            await task_to(TaskEvent.ATTEMPT_FAILED, f"failure:{failure.value}")
        if task.status is TaskStatus.DECIDING:
            await task_to(TaskEvent.REJECT, f"failure:{failure.value}")
    finally:
        if ws is not None and not req.keep_worktrees:
            with anyio.CancelScope(shield=True):
                await wm.remove_workspace(ws)
                await emit(
                    "workspace.removed",
                    ev.WorkspaceRemovedPayload(path=ws.path),
                    task_id=task.id,
                    attempt_id=attempt_id,
                )

    # ---- finalize -------------------------------------------------------------------------
    await run_to(RunEvent.ALL_TASKS_TERMINAL)
    if task.status is TaskStatus.COMPLETED:
        await run_to(RunEvent.COMPLETE)
        await emit("run.completed", ev.RunCompletedPayload(summary=f"merged into {branch.name}"))
    elif task.status is TaskStatus.CANCELLED:
        await run_to(RunEvent.CANCEL)
        await emit("run.cancelled", ev.RunCancelledPayload(reason="attempt cancelled"))
    else:
        await run_to(RunEvent.FAIL)
        await emit(
            "run.failed",
            ev.RunFailedPayload(
                failure=failure, reason=final_failure_note or (failure.value if failure else None)
            ),
        )

    return RunResult(
        run_id=run_id,
        status=run.status,
        branch=branch.name,
        task_id=task.id,
        attempt_id=attempt_id if ws is not None else None,
        agent_id=req.agent_id,
        failure=failure,
        claim=outcome.claim if outcome else None,
        diff=capture.summary if capture else None,
        usage=outcome.usage if outcome else Usage(),
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
