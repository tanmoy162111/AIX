"""Event envelope and per-type payload schemas (PLAYBOOK §8.3).

Every event type has exactly one frozen payload model; ``EVENT_PAYLOADS`` is the registry. Payloads
carry enough data for projections to be rebuilt by replay (§8.2) and never carry agent stream
output (that lives in ``.aix/runs/<run>/<attempt>.stream.jsonl``).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import Field

from aix.domain.artifacts import Artifact
from aix.domain.base import DomainModel, Sha256, UtcDatetime
from aix.domain.decisions import Approval, DecisionRecord
from aix.domain.enums import (
    AttemptStatus,
    CheckKind,
    DecisionPoint,
    FailureClass,
    RunStatus,
    TaskStatus,
)
from aix.domain.execution import Attempt, ExecutionResult
from aix.domain.ids import (
    ApprovalId,
    AttemptId,
    CheckId,
    DecisionId,
    EventId,
    RunId,
    TaskId,
)
from aix.domain.runs import Run
from aix.domain.state import RunEvent, TaskEvent
from aix.domain.tasks import Intent, Task, TaskGraph
from aix.domain.verification import Check, VerificationReport

SCHEMA_VERSION = 1
"""Payload schema version stored with every event; bump with a migration + ADR."""


class Payload(DomainModel):
    """Base class for event payloads."""


# ---- run ------------------------------------------------------------------


class RunCreatedPayload(Payload):
    run: Run


class RunPlannedPayload(Payload):
    intent: Intent
    graph: TaskGraph


class RunStateChangedPayload(Payload):
    from_status: RunStatus
    to_status: RunStatus
    event: RunEvent | None = None
    reason_codes: list[str] = Field(default_factory=list)


class RunCompletedPayload(Payload):
    summary: str | None = None


class RunFailedPayload(Payload):
    failure: FailureClass | None = None
    reason: str | None = None


class RunCancelledPayload(Payload):
    reason: str | None = None


# ---- task / attempt -----------------------------------------------------------


class TaskCreatedPayload(Payload):
    task: Task


class TaskStateChangedPayload(Payload):
    from_status: TaskStatus
    to_status: TaskStatus
    event: TaskEvent | None = None
    reason_codes: list[str] = Field(default_factory=list)


class AttemptCreatedPayload(Payload):
    attempt: Attempt


class AttemptStartedPayload(Payload):
    pid: int | None = None


class AttemptFinishedPayload(Payload):
    status: AttemptStatus
    result: ExecutionResult | None = None


# ---- agent ----------------------------------------------------------------


class AgentSelectedPayload(Payload):
    agent_id: str
    model: str | None = None
    fallbacks: list[str] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict)
    reason_codes: list[str] = Field(default_factory=list)


class AgentOutputPayload(Payload):
    """A rate-limited milestone summary, never raw stream output (§8.2)."""

    text: str = Field(max_length=2000)


class AgentToolCalledPayload(Payload):
    name: str
    ok: bool | None = None


class AgentFailedPayload(Payload):
    failure: FailureClass
    message: str = ""


# ---- workspace --------------------------------------------------------------


class WorkspaceCreatedPayload(Payload):
    path: Path
    branch: str
    base_commit: str


class WorkspaceMergedPayload(Payload):
    branch: str
    into: str
    commit: str


class WorkspaceConflictPayload(Payload):
    branch: str
    files: list[str] = Field(default_factory=list)


class WorkspaceRemovedPayload(Payload):
    path: Path


# ---- verification -----------------------------------------------------------------


class CheckStartedPayload(Payload):
    check_id: CheckId
    kind: CheckKind


class CheckFinishedPayload(Payload):
    check: Check


class VerificationCompletedPayload(Payload):
    report: VerificationReport


# ---- decisions and approvals --------------------------------------------------------


class DecisionRequestedPayload(Payload):
    decision_id: DecisionId
    point: DecisionPoint
    subject: str


class DecisionCompletedPayload(Payload):
    record: DecisionRecord


class ApprovalRequestedPayload(Payload):
    approval: Approval


class ApprovalGrantedPayload(Payload):
    approval_id: ApprovalId
    actor: str
    channel: Literal["cli_tty", "api_token"]
    decided_at: UtcDatetime


class ApprovalDeniedPayload(Payload):
    approval_id: ApprovalId
    actor: str
    channel: Literal["cli_tty", "api_token"]
    decided_at: UtcDatetime
    reason: str | None = None


class ApprovalExpiredPayload(Payload):
    approval_id: ApprovalId
    decided_at: UtcDatetime


# ---- misc -------------------------------------------------------------------------


class PolicyViolationPayload(Payload):
    kind: str
    detail: str
    path: str | None = None


class ArtifactCreatedPayload(Payload):
    artifact: Artifact


class BudgetExceededPayload(Payload):
    budget: Literal["cost_usd", "attempts", "wall_seconds"]
    limit: float
    actual: float


class ContextCompactedPayload(Payload):
    before_sha256: Sha256
    after_sha256: Sha256


EVENT_PAYLOADS: Mapping[str, type[Payload]] = MappingProxyType(
    {
        "run.created": RunCreatedPayload,
        "run.planned": RunPlannedPayload,
        "run.state_changed": RunStateChangedPayload,
        "run.completed": RunCompletedPayload,
        "run.failed": RunFailedPayload,
        "run.cancelled": RunCancelledPayload,
        "task.created": TaskCreatedPayload,
        "task.state_changed": TaskStateChangedPayload,
        "attempt.created": AttemptCreatedPayload,
        "attempt.started": AttemptStartedPayload,
        "attempt.finished": AttemptFinishedPayload,
        "agent.selected": AgentSelectedPayload,
        "agent.output": AgentOutputPayload,
        "agent.tool_called": AgentToolCalledPayload,
        "agent.failed": AgentFailedPayload,
        "workspace.created": WorkspaceCreatedPayload,
        "workspace.merged": WorkspaceMergedPayload,
        "workspace.conflict": WorkspaceConflictPayload,
        "workspace.removed": WorkspaceRemovedPayload,
        "check.started": CheckStartedPayload,
        "check.finished": CheckFinishedPayload,
        "verification.completed": VerificationCompletedPayload,
        "decision.requested": DecisionRequestedPayload,
        "decision.completed": DecisionCompletedPayload,
        "approval.requested": ApprovalRequestedPayload,
        "approval.granted": ApprovalGrantedPayload,
        "approval.denied": ApprovalDeniedPayload,
        "approval.expired": ApprovalExpiredPayload,
        "policy.violation": PolicyViolationPayload,
        "artifact.created": ArtifactCreatedPayload,
        "budget.exceeded": BudgetExceededPayload,
        "context.compacted": ContextCompactedPayload,
    }
)
"""Event type -> payload model."""


class Event(DomainModel):
    """A persisted event. ``seq`` is the global, gap-free append order."""

    seq: int = Field(ge=1)
    id: EventId
    run_id: RunId | None = None
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    type: str
    ts: UtcDatetime
    payload: Payload
    schema_version: int = SCHEMA_VERSION
