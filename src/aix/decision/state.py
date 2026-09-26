"""Decision state builders (PLAYBOOK §18.6, security-critical).

Jev does not treat its input as hostile, so text engineered into the state can move its answer.
State is therefore built **only from control-plane facts**: numbers, enums and short
control-plane-generated labels. Nothing here accepts agent prose (``ExecutionResult.claim``),
code, file contents, commit messages or finding descriptions. Every label is validated to
lowercase ``[a-z0-9_:.-]`` and at most 64 characters, so free text cannot be smuggled through a
string field.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Annotated, Final

from pydantic import Field, JsonValue, StringConstraints

from aix.domain.base import DomainModel, Risk
from aix.domain.enums import FailureClass, TaskType
from aix.domain.execution import DiffSummary
from aix.domain.tasks import Task, TaskGraph
from aix.domain.verification import Check, VerificationReport

Label = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_:.\-]{1,64}$")]
"""A control-plane label: lowercase, no spaces, at most 64 characters."""
PathLabel = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_./\-]{1,120}$")]
"""A workspace-relative path with no whitespace or punctuation beyond ``_./-``."""

METRICS: Final = frozenset(
    {
        "tests_total", "tests_passed", "tests_failed", "tests_skipped", "lint_errors",
        "type_errors", "policy_violations", "pre_existing", "findings_critical",
        "findings_high", "findings_medium", "findings_low",
    }
)  # fmt: skip
"""Metric names allowed into state; anything else a check reports is dropped."""


class TaskFacts(DomainModel):
    type: Label
    risk: Risk
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)


class CheckFacts(DomainModel):
    kind: Label
    status: Label
    required: bool
    metrics: dict[Label, float] = Field(default_factory=dict[Label, float])


class VerificationFacts(DomainModel):
    overall: Label
    checks: list[CheckFacts] = Field(default_factory=list[CheckFacts])


class DiffFacts(DomainModel):
    files_changed: int = Field(ge=0)
    lines_added: int = Field(ge=0)
    lines_removed: int = Field(ge=0)
    touches_scope_only: bool


class HistoryFacts(DomainModel):
    previous_failures: list[Label] = Field(default_factory=list[Label])
    agent_switched: bool = False


class ReviewFacts(DomainModel):
    reviewer_independent: bool | None = None
    findings_high: int = Field(default=0, ge=0)
    findings_medium: int = Field(default=0, ge=0)


class FailureFacts(DomainModel):
    failure_class: Label = Field(serialization_alias="class")
    sub_kind: Label | None = None
    candidates: list[Label] = Field(default_factory=list[Label])


class PlanFacts(DomainModel):
    risk: Risk
    tasks: int = Field(ge=0)
    write_tasks: int = Field(ge=0)
    write_tasks_without_checks: int = Field(ge=0)
    external_side_effect_types: list[Label] = Field(default_factory=list[Label])


class BudgetFacts(DomainModel):
    kind: Label
    used_ratio: float = Field(ge=0)
    tasks_completed: int = Field(ge=0)
    tasks_total: int = Field(ge=0)


class ToolFacts(DomainModel):
    classes: list[Label]
    target_paths: list[PathLabel] = Field(default_factory=list[PathLabel])
    task_risk: Risk
    in_scope: bool


class RunFacts(DomainModel):
    completed: int = Field(ge=0)
    failed: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    blocked: int = Field(ge=0)
    risk: Risk


class RoutingFacts(DomainModel):
    task_type: Label
    candidates: list[Label] = Field(min_length=1)
    """Agent ids tied within the routing margin (§12)."""


class DecisionState(DomainModel):
    """The compact, typed facts a decision provider may see. Sections are point specific."""

    task: TaskFacts | None = None
    verification: VerificationFacts | None = None
    diff: DiffFacts | None = None
    history: HistoryFacts | None = None
    review: ReviewFacts | None = None
    failure: FailureFacts | None = None
    plan: PlanFacts | None = None
    budget: BudgetFacts | None = None
    tool: ToolFacts | None = None
    run: RunFacts | None = None
    routing: RoutingFacts | None = None

    def to_wire(self) -> dict[str, JsonValue]:
        """Sparse JSON form sent to providers and stored in ``DecisionRecord.state``."""
        return self.model_dump(mode="json", exclude_none=True, by_alias=True)


def _check_facts(check: Check) -> CheckFacts:
    metrics = {k: v for k, v in check.metrics.items() if k in METRICS}
    return CheckFacts(
        kind=check.kind.value, status=check.status, required=check.required, metrics=metrics
    )


def _verification(report: VerificationReport) -> VerificationFacts:
    return VerificationFacts(
        overall=report.overall, checks=[_check_facts(c) for c in report.checks]
    )


def _failure_label(failure: FailureClass, sub_kind: str | None) -> str:
    return f"{failure.value}:{sub_kind}" if sub_kind else failure.value


def build_task_completion_state(
    task: Task,
    *,
    attempt: int,
    report: VerificationReport,
    diff: DiffSummary,
    touches_scope_only: bool,
    previous_failures: Sequence[tuple[FailureClass, str | None]] = (),
    agent_switched: bool = False,
    reviewer_independent: bool | None = None,
    review_findings: Mapping[str, int] | None = None,
) -> DecisionState:
    """State for ``task_completion`` (§18.6). Only counts, enums and labels; never a claim."""
    findings = review_findings or {}
    review = (
        ReviewFacts(
            reviewer_independent=reviewer_independent,
            findings_high=findings.get("high", 0),
            findings_medium=findings.get("medium", 0),
        )
        if reviewer_independent is not None or findings
        else None
    )
    return DecisionState(
        task=TaskFacts(
            type=task.type.value, risk=task.risk, attempt=attempt, max_attempts=task.max_attempts
        ),
        verification=_verification(report),
        diff=DiffFacts(
            files_changed=diff.files_changed,
            lines_added=diff.lines_added,
            lines_removed=diff.lines_removed,
            touches_scope_only=touches_scope_only,
        ),
        history=HistoryFacts(
            previous_failures=[_failure_label(f, s) for f, s in previous_failures],
            agent_switched=agent_switched,
        ),
        review=review,
    )


def build_failure_triage_state(
    task: Task,
    *,
    attempt: int,
    failure: FailureClass,
    sub_kind: str | None,
    candidates: Sequence[FailureClass],
    report: VerificationReport | None,
    previous_failures: Sequence[tuple[FailureClass, str | None]] = (),
    agent_switched: bool = False,
) -> DecisionState:
    """State for ``failure_triage``: the rule-produced class and the candidates Jev may choose."""
    return DecisionState(
        task=TaskFacts(
            type=task.type.value, risk=task.risk, attempt=attempt, max_attempts=task.max_attempts
        ),
        verification=_verification(report) if report else None,
        history=HistoryFacts(
            previous_failures=[_failure_label(f, s) for f, s in previous_failures],
            agent_switched=agent_switched,
        ),
        failure=FailureFacts(
            failure_class=failure.value,
            sub_kind=sub_kind,
            candidates=[c.value for c in candidates],
        ),
    )


def build_plan_review_state(graph: TaskGraph, *, risk: Risk) -> DecisionState:
    """State for ``plan_review``: counts and task-type labels of the plan."""
    writers = [t for t in graph.tasks if t.file_scope]
    return DecisionState(
        plan=PlanFacts(
            risk=risk,
            tasks=len(graph.tasks),
            write_tasks=len(writers),
            write_tasks_without_checks=sum(1 for t in writers if not t.verification.required),
            external_side_effect_types=sorted(
                {t.type.value for t in graph.tasks if t.type is TaskType.INTEGRATE}
            ),
        )
    )


def build_budget_state(
    kind: str, *, used: float, limit: float, tasks_completed: int, tasks_total: int
) -> DecisionState:
    """State for ``budget``: how much of which budget is used and how far the run got."""
    return DecisionState(
        budget=BudgetFacts(
            kind=kind,
            used_ratio=round(used / limit, 4) if limit > 0 else 0.0,
            tasks_completed=tasks_completed,
            tasks_total=tasks_total,
        )
    )


def build_tool_risk_state(
    *, classes: Sequence[str], target_paths: Sequence[str], task_risk: Risk, in_scope: bool
) -> DecisionState:
    """State for ``tool_risk``: control-plane command classes and workspace-relative paths."""
    return DecisionState(
        tool=ToolFacts(
            classes=list(classes),
            target_paths=list(target_paths),
            task_risk=task_risk,
            in_scope=in_scope,
        )
    )


def build_run_completion_state(
    *, completed: int, failed: int, cancelled: int, blocked: int, risk: Risk
) -> DecisionState:
    """State for ``run_completion``: task outcome counts."""
    return DecisionState(
        run=RunFacts(
            completed=completed, failed=failed, cancelled=cancelled, blocked=blocked, risk=risk
        )
    )


def build_routing_state(task_type: TaskType, candidates: Sequence[str]) -> DecisionState:
    """State for the ``routing`` tie-break: the task type and the tied agent ids."""
    return DecisionState(
        routing=RoutingFacts(task_type=task_type.value, candidates=list(candidates))
    )
