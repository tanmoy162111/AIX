"""Hard gates (PLAYBOOK §18.3): code that runs before any provider and cannot be overridden.

Each gate narrows the outcomes a provider may pick, or forces one. Providers only ever choose
among ``GateResult.allowed_outcomes``.
"""

from __future__ import annotations

from typing import Final

from pydantic import Field

from aix.domain.base import DomainModel
from aix.domain.decisions import GateResult
from aix.domain.enums import CheckKind
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P
from aix.domain.verification import VerificationReport

BASE_OUTCOMES: Final[dict[P, frozenset[O]]] = {
    P.TASK_COMPLETION: frozenset(
        {O.ACCEPT, O.RETRY, O.SWITCH_AGENT, O.ESCALATE, O.ASK_HUMAN, O.REJECT}
    ),
    P.FAILURE_TRIAGE: frozenset({O.RETRY, O.SWITCH_AGENT, O.ESCALATE, O.ASK_HUMAN, O.REJECT}),
    P.ROUTING: frozenset({O.CHOOSE}),
    P.TOOL_RISK: frozenset({O.ALLOW, O.DENY, O.ASK_HUMAN}),
    P.PLAN_REVIEW: frozenset({O.ACCEPT, O.ASK_HUMAN, O.REJECT}),
    P.BUDGET: frozenset({O.STOP, O.ASK_HUMAN}),
    P.RUN_COMPLETION: frozenset({O.ACCEPT, O.REJECT}),
}
"""Outcomes each decision point may produce before gates (§18.2)."""
_BUDGET_OUTCOMES: Final = frozenset({O.STOP, O.ASK_HUMAN})
_ORDER: Final = {o: i for i, o in enumerate(O)}


class GateFacts(DomainModel):
    """Control-plane facts the gates look at."""

    report: VerificationReport | None = None
    attempt: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    actions: list[str] = Field(default_factory=list[str])
    """Gated action names this decision concerns (e.g. ``push``, ``deploy``)."""
    approval_required_for: list[str] = Field(default_factory=list[str])
    budget_exhausted: bool = False
    escalation_remaining: bool = False


def _failed(report: VerificationReport, kind: CheckKind) -> bool:
    return any(c.kind is kind and c.status in ("failed", "error") for c in report.checks)


def evaluate_gates(point: P, facts: GateFacts) -> GateResult:
    """Apply every gate that concerns ``point``.

    Contract (§18.3): required check failed/error or an ``incomplete`` report removes ``accept``;
    a failed ``secrets`` or ``policy`` check forces ``reject``; reaching ``max_attempts`` leaves
    only ``escalate`` (if a step remains), ``ask_human`` and ``reject``; a gated action forces
    ``ask_human`` regardless of provider; an exhausted budget leaves only ``stop``/``ask_human``.
    The forced outcome is always within the allowed set.
    """
    allowed = set(BASE_OUTCOMES[point])
    reasons: list[str] = []
    forced: O | None = None
    report = facts.report if point in (P.TASK_COMPLETION, P.FAILURE_TRIAGE) else None

    if report is not None:
        if any(c.required and c.status in ("failed", "error") for c in report.checks):
            allowed.discard(O.ACCEPT)
            reasons.append("gate:required_check_failed")
        if report.overall == "incomplete":
            allowed.discard(O.ACCEPT)
            reasons.append("gate:report_incomplete")
        if _failed(report, CheckKind.SECRETS):
            forced = O.REJECT
            reasons.append("gate:secrets_finding")
        if _failed(report, CheckKind.POLICY):
            forced = O.REJECT
            reasons.append("gate:policy_violation")
    if point is P.TASK_COMPLETION and facts.attempt >= facts.max_attempts:
        keep = {O.ASK_HUMAN, O.REJECT}
        if facts.escalation_remaining:
            keep.add(O.ESCALATE)
        if report is not None and report.overall in ("passed", "warning"):
            keep.add(O.ACCEPT)  # a good result on the last attempt is still accepted
        else:
            reasons.append("gate:attempts_exhausted")
        allowed &= keep
    if facts.budget_exhausted and point is not P.BUDGET:
        allowed = set(_BUDGET_OUTCOMES)
        reasons.append("gate:budget_exhausted")
    if O.ASK_HUMAN in allowed:
        for action in facts.actions:
            if action in facts.approval_required_for:
                reasons.append(f"gate:approval_required:{action}")
                forced = O.ASK_HUMAN
    if forced is not None and forced not in allowed:
        forced = None  # e.g. a forced reject cannot survive an exhausted budget
    return GateResult(
        forced_outcome=forced,
        allowed_outcomes=sorted(allowed, key=lambda o: _ORDER[o]),
        reason_codes=reasons,
    )
