"""Multi-task run execution (PLAYBOOK §14): plan, route, schedule, attempt, integrate, finalize.

An attempt is accepted when the agent finished, stayed inside its file scope, changed something
(write tasks) and the verification report is ``passed`` or ``warning``. Until the Decision Service
and retries exist (M5) any other outcome rejects the attempt. The agent's claim is stored, never
used as a signal (§10.4).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

import anyio

from aix.agents.protocol import AgentAdapter, AgentHandle, AgentRequest
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
from aix.core.budget import BudgetTracker, Overrun
from aix.core.context.compaction import compact_context
from aix.core.context.facts import ProjectFacts, load_project_facts
from aix.core.context.handoff import build_handoff
from aix.core.context.prompt import render_task_prompt
from aix.core.cost import estimate_usage
from aix.core.escalation import Step as EscStep
from aix.core.failure import Classified, candidates_for, classify_attempt, classify_verification
from aix.core.orchestrator.attempt import (
    conflict_files,
    execute_agent,
    model_override,
    persist_artifacts,
    persist_prompt,
    timeout_override,
)
from aix.core.orchestrator.plan import PlanRunRequest, PlanRunResult, choose, plan_into
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.orchestrator.recovery import (
    INTERRUPTED_MUTATION,
    clear_pid,
    orchestrator_alive,
    recover_interrupted_run,
    write_pid,
)
from aix.core.orchestrator.task_policy import NextAction, TaskPolicy
from aix.core.planner.agent import make_adapter_runner
from aix.core.retry import RetryFingerprint, RetryLedger, remaining_mutations, split_task
from aix.core.router.rules import AgentStat, RoutingContext, RoutingDecision, route
from aix.core.router.stats import router_stats
from aix.core.scheduler.engine import Scheduler
from aix.core.toolrisk import SAFE_CLASSES, classify_command, gated_actions
from aix.core.workspace.manager import DiffCapture, Workspace, WorkspaceManager
from aix.core.workspace.scope import scope_violations
from aix.decision import state as dstate
from aix.decision.gates import GateFacts
from aix.decision.provider import DecisionProvider
from aix.decision.providers.rules import RulesProvider
from aix.decision.service import DecisionService
from aix.domain.agents import AgentSpec
from aix.domain.context import Handoff
from aix.domain.decisions import Approval, DecisionRecord
from aix.domain.enums import (
    RUN_TERMINAL,
    ArtifactType,
    AttemptStatus,
    DecisionOutcome,
    DecisionPoint,
    FailureClass,
    RetryMutation,
    RunStatus,
    TaskStatus,
    TaskType,
)
from aix.domain.enums import CheckKind as K
from aix.domain.errors import AixError, MergeConflict, NoEligibleAgent, classify
from aix.domain.execution import Attempt, ExecutionResult, ToolCallRecord, Usage
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import RunEvent, TaskEvent, transition_task
from aix.domain.tasks import Task, TaskGraph
from aix.domain.verification import Check, VerificationReport
from aix.security.approvals import ensure_token
from aix.security.policy import Policy
from aix.security.redact import add_known_secrets
from aix.security.sandbox import container_spec, ensure_container_ready
from aix.skills.registry import SkillRegistry
from aix.store import events as ev
from aix.store.db import EventStore
from aix.tools.git import git
from aix.verification.ai_review import pick_reviewer, run_ai_review
from aix.verification.baseline import Baseline, run_baseline
from aix.verification.commands import resolve_commands
from aix.verification.engine import CHECK_TIMEOUT_S, COMMAND_KINDS, Reviewer, run_verification

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
    detail: str | None = None
    """First failing check's summary, for humans."""


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
    pending_approvals: list[str] = field(default_factory=list[str])

    @property
    def exit_code(self) -> int:
        """PLAYBOOK §23.2: 0 success, 1 failed, 3 waiting for approval, 4 cancelled, 6 budget."""
        if self.failure is FailureClass.BUDGET_EXCEEDED:
            return 6
        return {RunStatus.COMPLETED: 0, RunStatus.CANCELLED: 4, RunStatus.WAITING_APPROVAL: 3}.get(
            self.status, 1
        )


def _utc() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Live:
    adapter: AgentAdapter
    handle: AgentHandle


@dataclass(frozen=True)
class _End:
    """How one attempt ended."""

    kind: Literal["completed", "failed", "cancelled", "conflict", "retry", "waiting"]
    failure: FailureClass | None = None
    files: list[str] = field(default_factory=list[str])
    action: NextAction | None = None
    cost: float | None = None


@dataclass
class _Book:
    """Per-task bookkeeping the outcome needs."""

    agent_id: str | None = None
    attempts: int = 0
    failure: FailureClass | None = None
    detail: str | None = None
    usage: Usage = field(default_factory=Usage)


async def emit_decision(rec: RunRecorder, record: DecisionRecord) -> None:
    """Record a decision as ``decision.requested`` + ``decision.completed``."""
    await rec.emit(
        "decision.requested",
        ev.DecisionRequestedPayload(
            decision_id=record.id, point=record.point, subject=record.subject
        ),
    )
    await rec.emit("decision.completed", ev.DecisionCompletedPayload(record=record))


async def request_approval(
    rec: RunRecorder, subject: str, action: str, reasons: Sequence[str]
) -> str:
    """Record a pending approval for ``subject``; ``aix approve|deny`` resolves it."""
    approval = Approval(
        id=new_id(IdPrefix.APPROVAL),
        subject=subject,
        action=action,
        scope={"reasons": list(reasons)},
        requested_at=rec.now(),
    )
    await rec.emit("approval.requested", ev.ApprovalRequestedPayload(approval=approval))
    return approval.id


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
        service: DecisionService,
        tracker: BudgetTracker,
        on_stop: Callable[[], None],
        backoff_scale: float = 1.0,
    ) -> None:
        self._service = service
        self._tracker = tracker
        self._on_stop = on_stop
        self._backoff_scale = backoff_scale
        self._policies: dict[str, TaskPolicy] = {}
        self._ledgers: dict[str, RetryLedger] = {}
        self._unavailable: set[str] = set()
        self.superseded: set[str] = set()
        self.stop_reason: FailureClass | None = None
        self.pending_approvals: list[str] = []
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
        self._policy = Policy(config.security)
        self._container = container_spec(config.security)
        self._handoffs: dict[str, Handoff] = {}
        self._resume_notes: dict[str, str] = {}
        self._router_stats: dict[tuple[str, TaskType], AgentStat] = {}
        self._facts: ProjectFacts | None = None
        self._skills: SkillRegistry | None = None
        self._baseline_lock = anyio.Lock()
        self._baseline_result: Baseline | None = None
        self.book: dict[str, _Book] = {t.id: _Book() for t in graph.tasks}
        for t in graph.tasks:
            self._register(t)

    def _register(self, task: Task) -> None:
        ladder = [EscStep(x) for x in self._config.execution.escalation_ladder]
        self._policies[task.id] = TaskPolicy(task.max_attempts, ladder, task.file_scope)
        self._ledgers[task.id] = RetryLedger()
        self.book.setdefault(task.id, _Book())

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

    def set_resume_notes(self, notes: dict[str, str]) -> None:
        """Tasks whose first attempt after a crash restarts interrupted work."""
        self._resume_notes = notes

    def set_router_stats(self, stats: dict[tuple[str, TaskType], AgentStat]) -> None:
        """Observed history from earlier runs, loaded once when the run starts."""
        self._router_stats = stats

    def _spec(self, agent_id: str) -> AgentSpec:
        return next(sp for sp in self._specs if sp.id == agent_id)

    def _route(
        self, task: Task, policy: TaskPolicy, exclude: set[str], force: str | None
    ) -> tuple[str, RoutingDecision | None]:
        """Pick an agent: a forced escalation target if usable, else the router's choice."""
        usable = [sp for sp in self._specs if sp.id not in self._unavailable]
        if force is not None and any(
            sp.id == force and sp.health in ("ready", "degraded") for sp in usable
        ):
            return force, None
        ctx = RoutingContext(
            task=task,
            agents=[sp for sp in usable if sp.id not in exclude],
            failed_agents=frozenset(policy.failed_agents),
            authored_by=frozenset(self._authors_for(task)),
            config=self._config.routing,
            stats=self._router_stats,
            policy_allows=lambda agent, t: bool(self._policy.can_run_agent(agent, t)),
        )
        try:
            decision = route(ctx)
        except NoEligibleAgent:
            if not exclude:
                raise
            decision = route(replace(ctx, agents=usable))  # nobody else can take over
        return decision.primary, decision

    async def run_task(self, task: Task) -> None:
        """Route and run attempts, deciding after each, until the task is terminal or waiting."""
        policy = self._policies[task.id]
        ledger = self._ledgers[task.id]
        notes: list[str] = []
        mutation: RetryMutation | None = None
        if (note := self._resume_notes.get(task.id)) is not None:
            notes.append(note)
            mutation = INTERRUPTED_MUTATION
        exclude: set[str] = set[str]()
        force: str | None = None
        model: str | None = None
        review = False
        wait_s = 0.0
        while True:
            task = self._tasks[task.id]
            if self._cancelled or self.stop_reason is not None:
                return  # the scheduler cancels whatever is still non-terminal
            if (over := self._tracker.can_start_attempt()) is not None:
                await self._budget_stop(over)
                return
            if wait_s > 0:
                await anyio.sleep(wait_s * self._backoff_scale)
                wait_s = 0.0
                if self._cancelled:
                    return
            try:
                agent_id, decision = self._route(task, policy, exclude, force)
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
            await self._rec.emit(
                "agent.selected",
                ev.AgentSelectedPayload(
                    agent_id=agent_id,
                    model=model or model_override(self._config, agent_id),
                    fallbacks=decision.fallbacks if decision else [],
                    scores=decision.scores if decision else {},
                    reason_codes=decision.reason_codes if decision else ["escalation:forced"],
                ),
                task_id=task.id,
            )
            await self.apply(task.id, TaskEvent.ASSIGN)
            policy.begin_attempt()
            self._tracker.start_attempt()
            book = self.book[task.id]
            book.agent_id = agent_id
            book.attempts += 1
            end = await self._attempt(
                task, agent_id, book.attempts, mutation, notes, ledger, policy,
                model=model, require_review=review,
            )  # fmt: skip
            self._tracker.add_cost(end.cost)
            exclude, force, model, review = set[str](), None, None, False
            if end.kind == "conflict":
                mutation = RetryMutation.REBASE_AND_RETRY
                notes.append(
                    "Your previous attempt could not be merged: it conflicted with changes already "
                    f"merged into the run branch in: {', '.join(end.files) or 'unknown files'}. "
                    "This workspace starts from the updated branch; redo the work on top of it."
                )
            elif end.kind == "retry" and end.action is not None:
                act = end.action
                mutation = act.mutation
                notes.extend(act.notes)
                if end.failure not in (
                    None,
                    FailureClass.AGENT_NO_CHANGES,
                    FailureClass.RATE_LIMITED,
                    FailureClass.NETWORK_FAILURE,
                    FailureClass.AUTH_FAILURE,
                ):
                    policy.failed_agents.add(agent_id)
                if act.exclude_agent:
                    exclude = {act.exclude_agent}
                if act.mark_unavailable:
                    self._unavailable.add(act.mark_unavailable)
                force, model, review, wait_s = (
                    act.force_agent,
                    act.model,
                    act.require_review,
                    act.wait_s,
                )
                same_agent = act.mutation is not None and act.mutation.value.startswith(
                    "same_agent"
                )
                if force is None and (same_agent or act.mutation is RetryMutation.WAIT_AND_RETRY):
                    force = agent_id  # the router's failed-agent penalty must not undo "same agent"
                if act.split:
                    await self._split(task)
                    return
            else:
                book.failure = end.failure
                if end.failure not in (None, FailureClass.AGENT_NO_CHANGES):
                    policy.failed_agents.add(agent_id)
            if (over := self._tracker.exceeded()) is not None:
                await self._budget_stop(over)
                return
            if end.kind not in ("conflict", "retry"):
                return

    async def _split(self, task: Task) -> None:
        """Replace a timed-out task with chained subtasks (§19.2 ``split_task``)."""
        dependents = [t for t in self._tasks.values() if task.id in t.depends_on]
        subs, rewired = split_task(task, dependents, parts=3)
        for d in rewired:
            self._tasks[d.id] = d
        for sub in subs:
            self._tasks[sub.id] = sub
            self._register(sub)
            await self._rec.emit("task.created", ev.TaskCreatedPayload(task=sub), task_id=sub.id)
        self.superseded.add(task.id)
        await self.apply(task.id, TaskEvent.CANCEL, "split_into:" + ",".join(s.id for s in subs))

    async def _budget_stop(self, over: Overrun) -> None:
        """Record a budget overrun, ask the ``budget`` decision point and stop the run."""
        if self.stop_reason is not None:
            return
        rec = self._rec
        await rec.emit(
            "budget.exceeded",
            ev.BudgetExceededPayload(budget=over.budget, limit=over.limit, actual=over.actual),
        )
        live = [t for t in self._tasks.values() if t.id not in self.superseded]
        state = dstate.build_budget_state(
            over.budget,
            used=over.actual,
            limit=over.limit,
            tasks_completed=sum(1 for t in live if t.status is TaskStatus.COMPLETED),
            tasks_total=len(live),
        )
        record = await self._service.decide(
            DecisionPoint.BUDGET, rec.run.id, state,
            GateFacts(attempt=1, max_attempts=1, budget_exhausted=True),
        )  # fmt: skip
        await self._emit_decision(record)
        if record.outcome is DecisionOutcome.ASK_HUMAN:
            await self._request_approval(rec.run.id, "budget_override", ["budget:" + over.budget])
        self.stop_reason = FailureClass.BUDGET_EXCEEDED
        self._on_stop()

    async def _emit_decision(self, record: DecisionRecord) -> None:
        await emit_decision(self._rec, record)

    async def _request_approval(self, subject: str, action: str, reasons: list[str]) -> str:
        approval_id = await request_approval(self._rec, subject, action, reasons)
        self.pending_approvals.append(approval_id)
        return approval_id

    async def _decide_attempt(
        self,
        task: Task,
        agent_id: str,
        policy: TaskPolicy,
        cls: Classified | None,
        report: VerificationReport | None,
        capture: DiffCapture,
        scope_ok: bool,
        detail: str,
        model: str | None,
    ) -> NextAction:
        """Gates, provider, record and the concrete next action for one finished attempt."""
        prev = [
            (c.failure, c.sub_kind.value if c.sub_kind else None) for c in policy.previous_failures
        ]
        facts = GateFacts(
            report=report,
            attempt=max(policy.consumed, 1),
            max_attempts=task.max_attempts,
            approval_required_for=list(self._config.security.approval_required_for),
            budget_exhausted=self._tracker.exceeded() is not None,
            escalation_remaining=policy.escalation_remaining,
        )
        if report is not None:
            point = DecisionPoint.TASK_COMPLETION
            state = dstate.build_task_completion_state(
                task,
                attempt=facts.attempt,
                report=report,
                diff=capture.summary,
                touches_scope_only=scope_ok,
                previous_failures=prev,
                agent_switched=policy.agent_switched,
            )
        else:
            assert cls is not None
            point = DecisionPoint.FAILURE_TRIAGE
            state = dstate.build_failure_triage_state(
                task,
                attempt=facts.attempt,
                failure=cls.failure,
                sub_kind=cls.sub_kind.value if cls.sub_kind else None,
                candidates=candidates_for(cls.failure),
                mutations=remaining_mutations(cls, policy.used.get(cls.label, [])),
                report=None,
                previous_failures=prev,
                agent_switched=policy.agent_switched,
            )
        record = await self._service.decide(point, task.id, state, facts)
        await self._emit_decision(record)
        return policy.plan_next(
            record.outcome,
            cls,
            current=self._spec(agent_id),
            current_model=model,
            candidates=[sp for sp in self._specs if sp.id not in self._unavailable],
            detail=detail,
        )

    def restore_authors(self, authors: dict[str, str]) -> None:
        """Re-seed who authored which merged task after a resume."""
        self._authors.update(authors)

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

    # ---- verification ---------------------------------------------------------------------

    async def _baseline(self) -> Baseline:
        """Checks on the untouched run branch, computed once before the first verification."""
        async with self._baseline_lock:
            if self._baseline_result is None:
                kinds = sorted(
                    {
                        k
                        for t in self._tasks.values()
                        if t.file_scope
                        for k in t.verification.required
                        if k in COMMAND_KINDS
                    },
                    key=lambda k: k.value,
                )
                run_id = self._rec.run.id
                gate_task = next(t for t in self._tasks.values() if t.file_scope)
                ws = await self._wm.create_attempt_workspace(run_id, new_id(IdPrefix.ATTEMPT))
                try:
                    self._baseline_result = await run_baseline(
                        ws.path,
                        resolve_commands(ws.path, self._config.verification.commands),
                        kinds,
                        allow=list(self._config.security.shell_allow),
                        timeout_s=CHECK_TIMEOUT_S,
                        network=self._config.security.network.checks,
                        command_gate=lambda argv: self._gate_command(gate_task, argv),
                    )
                finally:
                    with anyio.CancelScope(shield=True):
                        await self._wm.remove_workspace(ws)
                        await git(self._wm.root, "branch", "-D", ws.branch, check=False)
            return self._baseline_result

    async def _verify(
        self,
        task: Task,
        ws: Workspace,
        capture: DiffCapture,
        attempt_id: str,
        agent_id: str,
        stream_path: Path,
        require_review: bool = False,
    ) -> VerificationReport | None:
        """Run the verification engine for a write task and record every check (§17)."""
        if not task.file_scope:
            return None
        rec = self._rec
        reviewer = None
        spec = task.verification
        if require_review and K.AI_REVIEW not in spec.required:
            spec = spec.model_copy(update={"required": [*spec.required, K.AI_REVIEW]})
        if K.AI_REVIEW in spec.required or K.AI_REVIEW in spec.optional:
            reviewer = self._reviewer(task, agent_id)
        report = await run_verification(
            ws.path,
            attempt_id,
            spec,
            self._config,
            patch=capture.patch,
            changed_paths=capture.summary.paths,
            file_scope=task.file_scope,
            baseline=await self._baseline(),
            command_gate=lambda argv: self._gate_command(task, argv),
            out_dir=stream_path.parent / attempt_id / "verification",
            reviewer=reviewer,
            goal=task.goal,
        )
        for check in report.checks:
            await rec.emit(
                "check.finished",
                ev.CheckFinishedPayload(check=check),
                task_id=task.id,
                attempt_id=attempt_id,
            )
        await rec.emit(
            "verification.completed",
            ev.VerificationCompletedPayload(report=report),
            task_id=task.id,
            attempt_id=attempt_id,
        )
        return report

    async def _gate_command(self, task: Task, argv: list[str]) -> str | None:
        """``tool_risk`` for a control-plane command: ordinary build/test commands pass silently."""
        classes = classify_command(argv)
        if set(classes) <= SAFE_CLASSES:
            return None
        state = dstate.build_tool_risk_state(
            classes=classes, target_paths=[], task_risk=task.risk, in_scope=True
        )
        record = await self._service.decide(
            DecisionPoint.TOOL_RISK,
            task.id,
            state,
            GateFacts(
                attempt=1,
                max_attempts=1,
                actions=gated_actions(classes),
                approval_required_for=list(self._config.security.approval_required_for),
            ),
        )
        await self._emit_decision(record)
        if record.outcome is DecisionOutcome.ALLOW:
            return None
        return f"tool_risk {record.outcome.value} ({', '.join(classes)})"

    def _reviewer(self, task: Task, agent_id: str) -> Reviewer:
        async def review(diff: str, checks: Sequence[Check]) -> Check | None:
            try:
                reviewer_id = pick_reviewer(self._specs, {agent_id}, self._config.routing)
            except NoEligibleAgent:
                return None
            override = self._config.agents.overrides.get(reviewer_id)
            runner = make_adapter_runner(
                self._registry.get(reviewer_id),
                self._wm,
                self._rec.run.id,
                timeout_s=(override.timeout_s if override and override.timeout_s else None)
                or self._config.execution.attempt_timeout_s,
                model=override.model if override else None,
                container=self._container,
            )
            return await run_ai_review(task.goal, diff, checks, runner, reviewer_id=reviewer_id)

        return review

    # ---- one attempt ----------------------------------------------------------------------

    async def _attempt(
        self,
        task: Task,
        agent_id: str,
        number: int,
        mutation: RetryMutation | None,
        notes: Sequence[str],
        ledger: RetryLedger,
        policy: TaskPolicy,
        *,
        model: str | None,
        require_review: bool,
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
                model=model or model_override(config, agent_id),
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
            prompt = await self._task_prompt(task, notes)
            await persist_prompt(stream_path, prompt)
            ledger.record(RetryFingerprint.of(agent_id, prompt, ws.base_commit))
            agent_req = AgentRequest(
                attempt_id=attempt_id,
                workspace=ws.path,
                prompt=prompt,
                model=attempt.model,
                timeout_s=timeout_override(config, agent_id) or config.execution.attempt_timeout_s,
                permissions=self._policy.agent_permissions(task),
                container=self._container,
                stream_path=stream_path,
            )
            tool_calls: list[ToolCallRecord] = []
            normalized: list[str] = []

            def register(handle: AgentHandle) -> None:
                self._live[attempt_id] = _Live(adapter, handle)

            tool_violations: list[ev.PolicyViolationPayload] = []
            outcome = await execute_agent(
                adapter, agent_req, rec.emit, task.id, attempt_id, tool_calls, normalized,
                register, self._policy, tool_violations,
            )  # fmt: skip
            self._live.pop(attempt_id, None)
            outcome = outcome.model_copy(
                update={
                    "usage": estimate_usage(
                        outcome.usage,
                        attempt.model or self._spec(agent_id).default_model,
                        config.pricing,
                    )
                }
            )
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
                for path in violations:  # always recorded, even when a tool violation also fails it
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
                if tool_violations:  # the agent reached for something it must never touch
                    failure = FailureClass.POLICY_FAILURE
                elif violations:
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

            cost = outcome.usage.cost_usd
            if cancelled:
                await self.apply(task.id, TaskEvent.CANCEL, "attempt:cancelled")
                return _End("cancelled", cost=cost)
            process_failed = outcome.status != "completed"
            report: VerificationReport | None = None
            if process_failed:
                failure = classify_attempt(
                    agent_failure=outcome.failure, agent_failed=True, stderr=outcome.stderr_tail
                )
                await self.apply(
                    task.id, TaskEvent.ATTEMPT_FAILED, f"failure:{failure.value if failure else ''}"
                )
            else:
                await self.apply(task.id, TaskEvent.EXECUTION_FINISHED)
                if failure is None:
                    report = await self._verify(
                        task, ws, capture, attempt_id, agent_id, stream_path, require_review
                    )
                    if report is None:  # read-only task: no checks ran, and none were required
                        report = VerificationReport(
                            attempt_id=attempt_id, checks=[], overall="passed"
                        )
                await self.apply(
                    task.id,
                    TaskEvent.VERIFIED,
                    f"verification:{report.overall}" if report else "verification:skipped",
                )
            cls: Classified | None = Classified(failure) if failure else None
            detail = outcome.stderr_tail[-500:].strip() if process_failed else ""
            if report is not None and (vf := classify_verification(report)) is not None:
                cls = vf
                bad = next((c for c in report.checks if c.required and c.status != "passed"), None)
                detail = bad.summary if bad else report.overall
                failure = vf.failure
            if cls is not None:
                self.book[task.id].failure = cls.failure
                self.book[task.id].detail = detail or None
            action = await self._decide_attempt(
                task, agent_id, policy, cls, report, capture, scope_ok=failure is None
                or failure is not FailureClass.SCOPE_VIOLATION,
                detail=detail, model=attempt.model or self._spec(agent_id).default_model,
            )  # fmt: skip
            if action.kind == "accept":
                self._handoffs[task.id] = build_handoff(
                    task_id=task.id,
                    title=task.title,
                    diff=capture.summary,
                    checks=report.checks if report else [],
                    claim=outcome.claim,
                )
                await self.apply(task.id, TaskEvent.ACCEPT, "decision:accept")
                await self.apply(task.id, TaskEvent.INTEGRATE)
                end = await self._integrate(
                    task, ws, attempt_id, can_retry=policy.consumed < task.max_attempts
                )
                return replace(end, cost=cost)
            await self.apply(task.id, action.event, *action.reasons)
            failure_class = cls.failure if cls else failure
            if action.kind == "ask_human":
                await self._request_approval(task.id, "task_decision", list(action.reasons))
                return _End("waiting", failure_class, cost=cost)
            if action.kind == "fail":
                return _End("failed", failure_class, cost=cost)
            return _End("retry", failure_class, action=action, cost=cost)
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

    async def _task_prompt(self, task: Task, notes: Sequence[str]) -> str:
        """Appendix B.2 prompt: dependency handoffs, failure notes, project facts, skill."""
        if self._facts is None:
            self._facts = await load_project_facts(self._wm.root)
        instructions: str | None = None
        if task.skill:
            self._skills = self._skills or SkillRegistry.builtin()
            with contextlib.suppress(AixError):
                instructions = self._skills.get(task.skill).instructions
        budget = self._config.execution.context_budget_tokens
        deps = [self._handoffs[d] for d in task.depends_on if d in self._handoffs]
        compaction = await compact_context(deps, failures=notes, budget_tokens=budget // 2)
        if compaction is not None:
            await self._rec.emit(
                "context.compacted",
                ev.ContextCompactedPayload(
                    before_sha256=compaction.before_sha256, after_sha256=compaction.after_sha256
                ),
                task_id=task.id,
            )
        return render_task_prompt(
            task,
            handoffs=deps,
            compacted=compaction.result if compaction else None,
            failure_notes=notes,
            facts=self._facts,
            skill_instructions=instructions,
            budget_tokens=budget,
        )

    async def _integrate(self, task: Task, ws: object, attempt_id: str, *, can_retry: bool) -> _End:
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
            if can_retry:
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


@dataclass(frozen=True)
class ResumeState:
    """What a resumed run remembers from before it waited."""

    prior_attempts: int = 0
    prior_cost_usd: float = 0.0
    attempt_counts: dict[str, int] = field(default_factory=dict[str, int])
    authors: dict[str, str] = field(default_factory=dict[str, str])
    """task id -> agent that authored its merged change (reviewer independence)."""
    superseded: frozenset[str] = frozenset()
    """Tasks replaced by ``split_task`` (recorded in their cancel reason)."""
    crashed: bool = False
    """The orchestrator died: the run is still ``executing`` (or ``finalizing``), not waiting."""
    notes: dict[str, str] = field(default_factory=dict[str, str])
    """task id -> control-plane note for its next attempt (interrupted attempts)."""


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


def default_decision_service(config: AixConfig) -> DecisionService:
    """The rules provider, or Jev when configured and a key is present (rules always back it)."""
    rules = RulesProvider()
    provider: DecisionProvider = rules
    if config.decision.provider == "jev":
        try:
            from aix.decision.providers.jev import TypeSafeJevClient
            from aix.decision.providers.jev_provider import JevDecisionProvider
            from aix.decision.thresholds import Thresholds

            provider = JevDecisionProvider(
                TypeSafeJevClient(timeout_s=config.decision.jev.timeout_ms / 1000),
                rules,
                Thresholds(config.decision.thresholds),
            )
        except Exception:  # missing SDK or key: rules decide, the record says which provider ran
            provider = rules
    return DecisionService(
        provider,
        rules,
        policy_version=Policy(config.security).hash,
        timeout_s=config.decision.jev.timeout_ms / 1000,
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
    decisions: DecisionService | None = None,
    backoff_scale: float = 1.0,
    resume: ResumeState | None = None,
) -> RunOutcome:
    """Execute a planned run (``rec.run.status == planned``) until it ends or must wait.

    Contract: tasks run through the scheduler with ``max_parallel`` concurrency, each attempt in
    its own worktree; accepted write attempts are merged into ``aix/run/<id>`` one at a time. After
    every attempt the Decision Service (gates first, then the provider) picks accept / retry /
    switch / escalate / ask a human / reject; retries follow the §19.2 mutation sequences and the
    §19.3 ladder, and ``max_attempts`` and the run budget are enforced. A failed task blocks its
    dependents. ``cancel`` (or the ``aix cancel`` marker file) stops live agents and ends the run
    ``cancelled``. An exceeded budget records ``budget.exceeded``, asks the ``budget`` decision
    point and ends the run failed with ``BUDGET_EXCEEDED`` (exit 6). A task waiting for a human
    leaves the run ``waiting_approval`` (exit 3). With ``resume`` the run continues from a
    ``waiting_approval`` state: prior attempts and cost still count against the budget, tasks keep
    their statuses (granted ones are ``ready``) and per-task retry counters start fresh, because a
    human decision grants another round. The user's checked-out branch is never touched.
    """
    t0 = time.monotonic()
    add_known_secrets([ensure_token()])  # the approval token must never reach a written file
    cancel = cancel or anyio.Event()
    stop = anyio.Event()
    run_id = rec.run.id
    specs = await registry.probe_all()
    tracker = BudgetTracker(rec.run.budget)
    if resume is not None:
        tracker.attempts, tracker.cost_usd = resume.prior_attempts, resume.prior_cost_usd
    driver = _Driver(
        rec,
        wm,
        graph,
        specs,
        registry=registry,
        config=config,
        keep_worktrees=keep_worktrees,
        service=decisions or default_decision_service(config),
        tracker=tracker,
        on_stop=stop.set,
        backoff_scale=backoff_scale,
    )
    driver.set_router_stats(router_stats(await rec.store.agent_stats(by_model=False)))
    if resume is not None:
        for task_id, n in resume.attempt_counts.items():
            if task_id in driver.book:
                driver.book[task_id].attempts = n
        driver.restore_authors(resume.authors)
        driver.superseded |= set(resume.superseded)
        driver.set_resume_notes(resume.notes)
        if not resume.crashed:
            await rec.run_to(RunEvent.APPROVAL_GRANTED)
    else:
        await rec.run_to(RunEvent.START_EXECUTION)
    write_pid(wm.root, run_id)
    marker = cancel_marker(wm.root, run_id)

    async def watch_cancel() -> None:
        while not stop.is_set():
            if cancel.is_set() or await anyio.Path(marker).exists():
                cancel.set()
                stop.set()
                return
            await anyio.sleep(0.25)

    scheduler = Scheduler(
        graph.tasks, driver, max_parallel=max_parallel or config.execution.max_parallel
    )
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(watch_cancel)
            await scheduler.run(stop)
            tg.cancel_scope.cancel()
    except BaseException:
        with anyio.CancelScope(shield=True):
            await driver.cancel_running()
        raise
    finally:
        clear_pid(wm.root, run_id)

    final = driver.tasks()
    live = [t for t in final if t.id not in driver.superseded]
    branch = f"aix/run/{run_id}"
    failure: FailureClass | None = None
    waiting = [t for t in live if t.status is TaskStatus.WAITING_APPROVAL]
    if waiting and driver.stop_reason is None and not cancel.is_set():
        await rec.run_to(RunEvent.AWAIT_APPROVAL)
    else:
        if rec.run.status is not RunStatus.FINALIZING:  # a crash may have left it finalizing
            await rec.run_to(RunEvent.ALL_TASKS_TERMINAL)
        if driver.stop_reason is not None:
            failure = driver.stop_reason
            await rec.run_to(RunEvent.FAIL)
            await rec.emit(
                "run.failed", ev.RunFailedPayload(failure=failure, reason="budget exceeded")
            )
        elif all(t.status is TaskStatus.COMPLETED for t in live):
            await rec.run_to(RunEvent.COMPLETE)
            await rec.emit("run.completed", ev.RunCompletedPayload(summary=f"merged into {branch}"))
        elif cancel.is_set():
            await rec.run_to(RunEvent.CANCEL)
            await rec.emit("run.cancelled", ev.RunCancelledPayload(reason="cancelled by user"))
        else:
            failure = next(
                (
                    driver.book[t.id].failure
                    for t in live
                    if t.status is TaskStatus.FAILED and driver.book[t.id].failure
                ),
                FailureClass.AGENT_FAILURE,
            )
            await rec.run_to(RunEvent.FAIL)
            failed = [t.title for t in live if t.status is TaskStatus.FAILED]
            await rec.emit(
                "run.failed",
                ev.RunFailedPayload(failure=failure, reason=f"failed tasks: {', '.join(failed)}"),
            )

    if rec.run.status in (RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED):
        await write_run_artifacts(
            rec, wm.root, bundle=config.artifacts.bundle, policy_hash=Policy(config.security).hash
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
            detail=driver.book[t.id].detail,
        )
        for t in live
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
        pending_approvals=list(driver.pending_approvals),
    )


async def write_run_artifacts(
    rec: RunRecorder, root: Path, *, bundle: bool = False, policy_hash: str | None = None
) -> None:
    """Persist the run's standard artifacts and manifest (§21.3) under ``.aix/``."""
    from aix import __version__
    from aix.artifacts.report import build_report, render_html, render_markdown
    from aix.artifacts.standard import write_manifest, write_named, write_standard_artifacts
    from aix.artifacts.store import ArtifactWriter, ObjectStore

    run_id = rec.run.id
    writer = ArtifactWriter(
        ObjectStore(root / ".aix" / "artifacts" / "objects"),
        rec,
        aix_version=__version__,
        policy_hash=policy_hash,
    )
    entries = await write_standard_artifacts(
        rec.store, writer, run_id, runs_dir=root / ".aix" / "runs" / run_id
    )
    report = await build_report(rec.store, run_id, artifact_names=[e.name for e in entries])
    entries.append(
        await write_named(
            writer, "report.md", ArtifactType.REPORT, render_markdown(report).encode(),
            "text/markdown",
        )
    )  # fmt: skip
    entries.append(
        await write_named(
            writer, "report.html", ArtifactType.REPORT, render_html(report).encode(), "text/html"
        )
    )
    await write_manifest(writer, run_id, entries)
    if bundle:
        from aix.artifacts.bundle import export_bundle

        await export_bundle(rec.store, root, run_id)


async def _plan_review(
    rec: RunRecorder,
    planned: PlanRunResult,
    service: DecisionService,
    config: AixConfig,
    *,
    t0: float,
) -> RunOutcome | None:
    """The ``plan_review`` decision for a high-risk intent (§18.2).

    ``None`` means go ahead. ``ask_human`` records a pending approval and leaves the run
    ``waiting_approval`` (exit 3, nothing executed); ``reject`` fails the run.
    """
    state = dstate.build_plan_review_state(planned.graph, risk=planned.intent.risk)
    record = await service.decide(
        DecisionPoint.PLAN_REVIEW,
        rec.run.id,
        state,
        GateFacts(attempt=1, max_attempts=1),
    )
    await emit_decision(rec, record)
    if record.outcome is DecisionOutcome.ACCEPT:
        return None
    pending: list[str] = []
    await rec.run_to(RunEvent.AWAIT_APPROVAL)
    failure: FailureClass | None = None
    if record.outcome is DecisionOutcome.ASK_HUMAN:
        pending.append(await request_approval(rec, rec.run.id, "plan_review", record.reason_codes))
    else:
        failure = FailureClass.POLICY_FAILURE
        await rec.run_to(RunEvent.FAIL)
        await rec.emit("run.failed", ev.RunFailedPayload(failure=failure, reason="plan rejected"))
    summaries = [
        TaskSummary(t.id, t.title, t.type.value, TaskStatus.CREATED, None, 0, None)
        for t in planned.graph.tasks
    ]
    return RunOutcome(
        run_id=rec.run.id,
        status=rec.run.status,
        branch=f"aix/run/{rec.run.id}",
        planner=planned.planner,
        tasks=summaries,
        failure=failure,
        usage=Usage(),
        duration_ms=int((time.monotonic() - t0) * 1000),
        warnings=list(planned.warnings),
        pending_approvals=pending,
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
    decisions: DecisionService | None = None,
) -> RunOutcome:
    """Plan a goal and execute the plan (``aix run "<goal>"``).

    Raises:
        ConfigError: unknown skill or planner agent.
        ToolFailure: not a git repository, or a dirty tree without ``allow_dirty`` (nothing is
            recorded).
    """

    skills = skills or SkillRegistry.builtin()
    await ensure_container_ready(config.security)  # fail before anything is recorded
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
    service = decisions or default_decision_service(config)
    if planned.intent.risk == "high":
        held = await _plan_review(rec, planned, service, config, t0=time.monotonic())
        if held is not None:
            return held
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
        decisions=service,
    )


async def resume_run(
    run_id: str,
    *,
    project_root: Path,
    registry: AdapterRegistry,
    store: EventStore,
    config: AixConfig,
    clock: Callable[[], datetime] = _utc,
    cancel: anyio.Event | None = None,
    decisions: DecisionService | None = None,
    keep_worktrees: bool = False,
    backoff_scale: float = 1.0,
) -> RunOutcome:
    """Continue a ``waiting_approval`` run after its approvals were resolved (``aix approve``).

    Raises:
        ConfigError: unknown run, or the run is not waiting for approval.
    """
    return await _resume(
        run_id, project_root=project_root, registry=registry, store=store, config=config,
        clock=clock, cancel=cancel, decisions=decisions, keep_worktrees=keep_worktrees,
        backoff_scale=backoff_scale, crashed=False,
    )  # fmt: skip


async def resume_interrupted_run(
    run_id: str,
    *,
    project_root: Path,
    registry: AdapterRegistry,
    store: EventStore,
    config: AixConfig,
    clock: Callable[[], datetime] = _utc,
    cancel: anyio.Event | None = None,
    decisions: DecisionService | None = None,
    keep_worktrees: bool = False,
    backoff_scale: float = 1.0,
) -> RunOutcome:
    """Continue a run whose orchestrator died (``aix run --resume``), PLAYBOOK §8.2.

    Attempts that were mid-flight become ``failed`` with ``INTERRUPTED`` (their worktrees are
    removed), stranded tasks return to ``ready`` and normal retry logic continues; work already
    merged into the run branch is kept. A ``planned`` run that never started simply executes.

    Raises:
        ConfigError: unknown run; already finished; waiting for approval (use ``aix approve``);
            interrupted while planning (start a new run); or its orchestrator is still alive.
    """
    from aix.domain.errors import ConfigError

    run = await store.get_run(run_id)
    if run is None:
        raise ConfigError(f"unknown run {run_id!r}")
    if run.status in RUN_TERMINAL:
        raise ConfigError(f"run {run_id} already {run.status.value}; nothing to resume")
    if run.status is RunStatus.WAITING_APPROVAL:
        raise ConfigError(
            f"run {run_id} is waiting for approval: use `aix approvals` / `aix approve`"
        )
    if run.status in (RunStatus.CREATED, RunStatus.PLANNING):
        raise ConfigError(f"run {run_id} was interrupted while planning; start a new run")
    if (pid := orchestrator_alive(project_root, run_id)) is not None:
        raise ConfigError(f"run {run_id} is still being executed by process {pid}")
    return await _resume(
        run_id, project_root=project_root, registry=registry, store=store, config=config,
        clock=clock, cancel=cancel, decisions=decisions, keep_worktrees=keep_worktrees,
        backoff_scale=backoff_scale, crashed=True,
    )  # fmt: skip


async def _resume(
    run_id: str,
    *,
    project_root: Path,
    registry: AdapterRegistry,
    store: EventStore,
    config: AixConfig,
    clock: Callable[[], datetime],
    cancel: anyio.Event | None,
    decisions: DecisionService | None,
    keep_worktrees: bool,
    backoff_scale: float,
    crashed: bool,
) -> RunOutcome:
    from aix.domain.errors import ConfigError

    run = await store.get_run(run_id)
    if run is None:
        raise ConfigError(f"unknown run {run_id!r}")
    await ensure_container_ready(config.security)
    wm = WorkspaceManager(project_root)
    rec = RunRecorder(store, run, clock)
    notes: dict[str, str] = {}
    if crashed:
        if run.status is RunStatus.PLANNED:
            crashed = False  # never started: a plain execution of the recorded plan
        else:
            notes = (await recover_interrupted_run(rec, wm)).notes
    elif run.status is not RunStatus.WAITING_APPROVAL:
        raise ConfigError(f"run {run_id} is {run.status.value}, not waiting for approval")
    tasks = await store.get_tasks(run_id)
    counts: dict[str, int] = {}
    authors: dict[str, str] = {}
    total_attempts, total_cost = 0, 0.0
    for t in tasks:
        attempts = await store.get_attempts(t.id)
        counts[t.id] = len(attempts)
        total_attempts += len(attempts)
        for a in attempts:
            result = await store.get_result(a.id)
            total_cost += (result.usage.cost_usd or 0.0) if result else 0.0
        if attempts and t.status is TaskStatus.COMPLETED and t.file_scope:
            authors[t.id] = attempts[-1].agent_id
    superseded: dict[str, list[str]] = {}
    for e in await store.events(run_id=run_id, types=["task.state_changed"]):
        codes = getattr(e.payload, "reason_codes", [])
        for code in codes:
            if code.startswith("split_into:") and e.task_id:
                superseded[e.task_id] = code.removeprefix("split_into:").split(",")
    if superseded:  # dependents were re-wired in memory only; rebuild that from the record
        last = {orig: subs[-1] for orig, subs in superseded.items()}
        tasks = [
            t.model_copy(update={"depends_on": [last.get(d, d) for d in t.depends_on]})
            for t in tasks
        ]
    assert run.graph_id is not None
    graph = TaskGraph(id=run.graph_id, run_id=run_id, tasks=tasks)
    resume = None
    if crashed or run.status is RunStatus.WAITING_APPROVAL:
        resume = ResumeState(
            total_attempts, total_cost, counts, authors, frozenset(superseded), crashed, notes
        )
    return await execute_graph(
        rec,
        wm,
        graph,
        registry=registry,
        config=config,
        planner="resumed",
        keep_worktrees=keep_worktrees,
        cancel=cancel,
        decisions=decisions,
        backoff_scale=backoff_scale,
        resume=resume,
    )
