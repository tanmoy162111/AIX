"""Enumerations shared across the system. All values are lowercase snake_case (PLAYBOOK §4)."""

from __future__ import annotations

from enum import StrEnum


class TaskType(StrEnum):
    """Kind of work a task performs (§6)."""

    INSPECT = "inspect"
    RESEARCH = "research"
    DESIGN = "design"
    IMPLEMENT = "implement"
    TEST = "test"
    REVIEW = "review"
    SECURITY_REVIEW = "security_review"
    DOCUMENT = "document"
    INTEGRATE = "integrate"


class Capability(StrEnum):
    """Vocabulary of agent capabilities (§10.2, §11, §13.3)."""

    IMPLEMENT = "implement"
    DEBUG = "debug"
    TEST = "test"
    REVIEW = "review"
    DESIGN = "design"
    RESEARCH = "research"
    SUMMARIZE = "summarize"
    SECURITY = "security"
    DOCUMENT = "document"


class CheckKind(StrEnum):
    """Kind of verification check (§6, §17.3)."""

    BUILD = "build"
    TESTS = "tests"
    LINT = "lint"
    TYPECHECK = "typecheck"
    SECURITY_SAST = "security_sast"
    SECRETS = "secrets"
    DEPS = "deps"
    AI_REVIEW = "ai_review"
    POLICY = "policy"
    CUSTOM = "custom"


class FailureClass(StrEnum):
    """Failure taxonomy (§19.1). Every typed error maps to exactly one class."""

    AGENT_FAILURE = "agent_failure"
    AGENT_NO_CHANGES = "agent_no_changes"
    TOOL_FAILURE = "tool_failure"
    NETWORK_FAILURE = "network_failure"
    AUTH_FAILURE = "auth_failure"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    VERIFICATION_FAILURE = "verification_failure"
    SCOPE_VIOLATION = "scope_violation"
    POLICY_FAILURE = "policy_failure"
    MERGE_CONFLICT = "merge_conflict"
    CONTEXT_FAILURE = "context_failure"
    RESOURCE_FAILURE = "resource_failure"
    BUDGET_EXCEEDED = "budget_exceeded"
    NO_ELIGIBLE_AGENT = "no_eligible_agent"
    INTERRUPTED = "interrupted"
    HUMAN_REJECTION = "human_rejection"


class VerificationFailureKind(StrEnum):
    """Sub-kind of ``FailureClass.VERIFICATION_FAILURE`` (§19.1)."""

    TESTS = "tests"
    BUILD = "build"
    LINT = "lint"
    TYPECHECK = "typecheck"
    SECURITY = "security"
    REVIEW = "review"


class RetryMutation(StrEnum):
    """Why a retry attempt differs from the previous one (§19.2)."""

    SAME_AGENT_WITH_FAILURE_CONTEXT = "same_agent_with_failure_context"
    SAME_AGENT_WITH_FINDINGS = "same_agent_with_findings"
    SAME_AGENT_CLARIFIED_PROMPT = "same_agent_clarified_prompt"
    SAME_AGENT_WITH_SCOPE_REMINDER = "same_agent_with_scope_reminder"
    SAME_AGENT_NEW_CONTEXT = "same_agent_new_context"
    SWITCH_AGENT = "switch_agent"
    ADD_RESEARCH_STEP = "add_research_step"
    SPLIT_TASK = "split_task"
    WAIT_AND_RETRY = "wait_and_retry"
    REBASE_AND_RETRY = "rebase_and_retry"
    COMPACT_CONTEXT_AND_RETRY = "compact_context_and_retry"
    MORE_VERIFICATION = "more_verification"
    ASK_HUMAN = "ask_human"


class DecisionPoint(StrEnum):
    """Where the Decision Service is consulted (§18.2)."""

    TASK_COMPLETION = "task_completion"
    FAILURE_TRIAGE = "failure_triage"
    ROUTING = "routing"
    TOOL_RISK = "tool_risk"
    PLAN_REVIEW = "plan_review"
    BUDGET = "budget"
    RUN_COMPLETION = "run_completion"


class DecisionOutcome(StrEnum):
    """Outcome of a decision (§6). ``CHOOSE`` carries its argument in ``DecisionRecord.choice``."""

    ACCEPT = "accept"
    RETRY = "retry"
    REJECT = "reject"
    ESCALATE = "escalate"
    SWITCH_AGENT = "switch_agent"
    ASK_HUMAN = "ask_human"
    STOP = "stop"
    ALLOW = "allow"
    DENY = "deny"
    CHOOSE = "choose"


class RunStatus(StrEnum):
    """Run lifecycle states (§7.1)."""

    CREATED = "created"
    PLANNING = "planning"
    PLANNED = "planned"
    EXECUTING = "executing"
    WAITING_APPROVAL = "waiting_approval"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStatus(StrEnum):
    """Task lifecycle states (§7.2)."""

    CREATED = "created"
    READY = "ready"
    ASSIGNED = "assigned"
    RUNNING = "running"
    VERIFYING = "verifying"
    DECIDING = "deciding"
    ACCEPTED = "accepted"
    INTEGRATING = "integrating"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    """Attempt lifecycle states."""

    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


RUN_TERMINAL: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)
TASK_TERMINAL: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)
ATTEMPT_TERMINAL: frozenset[AttemptStatus] = frozenset(
    {AttemptStatus.COMPLETED, AttemptStatus.FAILED, AttemptStatus.CANCELLED}
)


class ArtifactType(StrEnum):
    """Kind of durable artifact (§21.3)."""

    PLAN = "plan"
    PATCH = "patch"
    VERIFICATION = "verification"
    LOG = "log"
    JUNIT = "junit"
    SARIF = "sarif"
    DECISION_LOG = "decision_log"
    AGENT_TRACE = "agent_trace"
    REPORT = "report"
    MANIFEST = "manifest"
    PROMPT = "prompt"
    STREAM = "stream"
    BASELINE = "baseline"
    OTHER = "other"
