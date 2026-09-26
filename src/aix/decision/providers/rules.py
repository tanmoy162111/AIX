"""Deterministic ``rules`` decision provider (PLAYBOOK §18.4): one table per decision point."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aix.decision.provider import ProviderAnswer, ProviderName
from aix.decision.state import DecisionState
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P

_HUMAN_ONLY: Final = frozenset({"policy_failure", "budget_exceeded", "human_rejection"})
_SWITCH_NOW: Final = frozenset({"auth_failure"})
_TRANSIENT: Final = frozenset({"rate_limited", "network_failure"})
_GATED_TOOLS: Final = frozenset(
    {"git_push", "deploy", "db_migration_apply", "secrets_write", "external_network"}
)
_SAFE_TOOLS: Final = frozenset({"read_only", "local_write"})
_FALLBACK_ORDER: Final = (O.ESCALATE, O.ASK_HUMAN, O.REJECT, O.STOP)
NEAR_DONE_RATIO: Final = 0.8
BUDGET_TOLERANCE: Final = 1.25


def _pick(prefer: Sequence[O], allowed: set[O], why: str) -> tuple[O, str]:
    """First preferred outcome that is allowed, else the safest allowed one."""
    for outcome in prefer:
        if outcome in allowed:
            return outcome, why
    for outcome in _FALLBACK_ORDER:
        if outcome in allowed:
            return outcome, f"{why}:constrained"
    return sorted(allowed, key=lambda o: o.value)[0], f"{why}:constrained"


def _repeats(state: DecisionState) -> bool:
    """A failure already happened and the agent has not been switched yet."""
    h = state.history
    return bool(h and h.previous_failures and not h.agent_switched)


def _task_completion(state: DecisionState, allowed: set[O]) -> tuple[O, str]:
    v, t = state.verification, state.task
    if v is None or t is None:
        return _pick([O.ASK_HUMAN], allowed, "missing_state")
    counts = {"medium": 0.0, "high": 0.0, "critical": 0.0}
    for c in v.checks:
        for sev in counts:
            counts[sev] += c.metrics.get(f"findings_{sev}", 0.0)
    if v.overall == "passed":
        return _pick([O.ACCEPT], allowed, "passed")
    if v.overall == "warning":
        if counts["high"] or counts["critical"]:
            return _pick([O.RETRY, O.SWITCH_AGENT], allowed, "warning_high_findings")
        if counts["medium"]:
            if t.attempt == 1:
                return _pick([O.RETRY, O.ACCEPT], allowed, "warning_medium_retry_once")
            return _pick([O.ACCEPT], allowed, "warning_medium_accept_with_note")
        return _pick([O.ACCEPT], allowed, "warning_low_only")
    if t.attempt >= t.max_attempts:
        return _pick([O.ESCALATE, O.ASK_HUMAN, O.REJECT], allowed, "attempts_exhausted")
    if v.overall == "incomplete":
        return _pick([O.RETRY, O.ASK_HUMAN], allowed, "incomplete_more_verification")
    if _repeats(state):
        return _pick([O.SWITCH_AGENT, O.RETRY], allowed, "repeated_failure")
    return _pick([O.RETRY, O.SWITCH_AGENT], allowed, "failed_retry")


def _failure_triage(state: DecisionState, allowed: set[O]) -> tuple[O, str]:
    f, t = state.failure, state.task
    if f is None or t is None:
        return _pick([O.ASK_HUMAN], allowed, "missing_state")
    cls = f.failure_class
    history = state.history.previous_failures if state.history else []
    same = sum(1 for p in history if p.split(":")[0] == cls)
    if cls in _HUMAN_ONLY:
        return _pick([O.ASK_HUMAN, O.REJECT], allowed, f"{cls}_needs_human")
    if cls in _SWITCH_NOW:
        return _pick([O.SWITCH_AGENT, O.ASK_HUMAN], allowed, f"{cls}_switch")
    if cls in _TRANSIENT:
        prefer = [O.RETRY] if same < 2 else [O.SWITCH_AGENT, O.RETRY]
        return _pick(prefer, allowed, f"{cls}_transient")
    if t.attempt >= t.max_attempts:
        return _pick([O.ESCALATE, O.ASK_HUMAN, O.REJECT], allowed, "attempts_exhausted")
    if same >= 1 and not (state.history and state.history.agent_switched):
        return _pick([O.SWITCH_AGENT, O.RETRY], allowed, f"{cls}_repeated")
    return _pick([O.RETRY, O.SWITCH_AGENT], allowed, f"{cls}_retry")


def _tool_risk(state: DecisionState, allowed: set[O]) -> tuple[O, str]:
    tool = state.tool
    if tool is None:
        return _pick([O.ASK_HUMAN, O.DENY], allowed, "missing_state")
    classes = set(tool.classes)
    if not tool.in_scope:
        return _pick([O.DENY, O.ASK_HUMAN], allowed, "outside_scope")
    if classes & _GATED_TOOLS:
        return _pick([O.ASK_HUMAN, O.DENY], allowed, "gated_action")
    if classes <= _SAFE_TOOLS:
        return _pick([O.ALLOW], allowed, "safe_tool")
    return _pick([O.ASK_HUMAN, O.DENY], allowed, "irreversible_or_unknown")


def _plan_review(state: DecisionState, allowed: set[O]) -> tuple[O, str]:
    plan = state.plan
    if plan is None:
        return _pick([O.ASK_HUMAN], allowed, "missing_state")
    if plan.external_side_effect_types:
        return _pick([O.ASK_HUMAN, O.REJECT], allowed, "external_side_effects")
    if plan.risk == "high":
        return _pick([O.ASK_HUMAN, O.REJECT], allowed, "high_risk_intent")
    if plan.write_tasks_without_checks:
        return _pick([O.REJECT, O.ASK_HUMAN], allowed, "write_task_without_checks")
    return _pick([O.ACCEPT], allowed, "plan_ok")


def _budget(state: DecisionState, allowed: set[O]) -> tuple[O, str]:
    b = state.budget
    if b is None:
        return _pick([O.STOP, O.ASK_HUMAN], allowed, "missing_state")
    near_done = b.tasks_total > 0 and b.tasks_completed / b.tasks_total >= NEAR_DONE_RATIO
    if near_done and b.used_ratio <= BUDGET_TOLERANCE:
        return _pick([O.ASK_HUMAN, O.STOP], allowed, "budget_near_done")
    return _pick([O.STOP, O.ASK_HUMAN], allowed, "budget_exceeded")


def _run_completion(state: DecisionState, allowed: set[O]) -> tuple[O, str]:
    r = state.run
    if r is None:
        return _pick([O.REJECT], allowed, "missing_state")
    if r.failed or r.blocked or r.cancelled:
        return _pick([O.REJECT], allowed, "tasks_not_completed")
    return _pick([O.ACCEPT], allowed, "all_completed")


class RulesProvider:
    """The default provider: deterministic, offline, always available."""

    name: ProviderName = "rules"

    async def decide(self, point: P, state: DecisionState, allowed: set[O]) -> ProviderAnswer:
        """Apply the table for ``point``; the outcome is always in ``allowed``."""
        if point is P.ROUTING:
            candidates = sorted(state.routing.candidates) if state.routing else []
            if not candidates:
                return ProviderAnswer(outcome=O.CHOOSE, reason_codes=["rules:no_candidates"])
            return ProviderAnswer(
                outcome=O.CHOOSE, choice=candidates[0], reason_codes=["rules:first_candidate"]
            )
        table = {
            P.TASK_COMPLETION: _task_completion,
            P.FAILURE_TRIAGE: _failure_triage,
            P.TOOL_RISK: _tool_risk,
            P.PLAN_REVIEW: _plan_review,
            P.BUDGET: _budget,
            P.RUN_COMPLETION: _run_completion,
        }[point]
        outcome, why = table(state, allowed)
        return ProviderAnswer(outcome=outcome, reason_codes=[f"rules:{why}"])
