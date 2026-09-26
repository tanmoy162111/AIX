from __future__ import annotations

import itertools

import pytest

from aix.decision.gates import BASE_OUTCOMES, GateFacts, evaluate_gates
from aix.domain.enums import CheckKind as K
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check, VerificationReport, compute_overall


def chk(kind: K, status: str, required: bool = True) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK), kind=kind, status=status,  # type: ignore[arg-type]
        required=required, summary="s",
    )  # fmt: skip


def report(*checks: Check) -> VerificationReport:
    return VerificationReport(
        attempt_id=new_id(IdPrefix.ATTEMPT), checks=list(checks), overall=compute_overall(checks)
    )


REPORTS: dict[str, VerificationReport | None] = {
    "none": None,
    "passed": report(chk(K.BUILD, "passed"), chk(K.TESTS, "passed")),
    "warning": report(chk(K.BUILD, "passed"), chk(K.LINT, "warning", required=False)),
    "required_failed": report(chk(K.TESTS, "failed")),
    "required_error": report(chk(K.BUILD, "error")),
    "optional_failed": report(chk(K.BUILD, "passed"), chk(K.SECURITY_SAST, "failed", False)),
    "incomplete": report(chk(K.TESTS, "passed"), chk(K.LINT, "skipped")),
    "secrets": report(chk(K.BUILD, "passed"), chk(K.SECRETS, "failed")),
    "policy": report(chk(K.BUILD, "passed"), chk(K.POLICY, "failed")),
}


def gate(
    point: P = P.TASK_COMPLETION, rep: str = "passed", **kw: object
) -> tuple[list[O], O | None, list[str]]:
    facts = GateFacts(report=REPORTS[rep], attempt=1, max_attempts=3, **kw)  # type: ignore[arg-type]
    g = evaluate_gates(point, facts)
    return g.allowed_outcomes, g.forced_outcome, g.reason_codes


def test_clean_pass_allows_everything_the_point_allows() -> None:
    allowed, forced, reasons = gate()
    assert set(allowed) == BASE_OUTCOMES[P.TASK_COMPLETION] and forced is None and reasons == []


@pytest.mark.parametrize("rep", ["required_failed", "required_error"])
def test_required_failure_forbids_accept(rep: str) -> None:
    allowed, forced, reasons = gate(rep=rep)
    assert O.ACCEPT not in allowed and forced is None
    assert "gate:required_check_failed" in reasons and O.RETRY in allowed


def test_incomplete_forbids_accept_but_allows_ask_human_and_retry() -> None:
    allowed, _, reasons = gate(rep="incomplete")
    assert O.ACCEPT not in allowed and {O.ASK_HUMAN, O.RETRY} <= set(allowed)
    assert "gate:report_incomplete" in reasons


@pytest.mark.parametrize(
    ("rep", "code"), [("secrets", "gate:secrets_finding"), ("policy", "gate:policy_violation")]
)
def test_secrets_and_policy_force_reject(rep: str, code: str) -> None:
    allowed, forced, reasons = gate(rep=rep)
    assert forced is O.REJECT and O.REJECT in allowed and O.ACCEPT not in allowed
    assert code in reasons


def test_optional_failure_and_warning_do_not_block_accept() -> None:
    assert O.ACCEPT in gate(rep="optional_failed")[0]
    assert O.ACCEPT in gate(rep="warning")[0]


def test_attempts_exhausted_limits_to_escalate_ask_human_reject() -> None:
    facts = GateFacts(
        report=REPORTS["required_failed"], attempt=3, max_attempts=3, escalation_remaining=True
    )
    g = evaluate_gates(P.TASK_COMPLETION, facts)
    assert set(g.allowed_outcomes) == {O.ESCALATE, O.ASK_HUMAN, O.REJECT}
    assert "gate:attempts_exhausted" in g.reason_codes
    no_esc = evaluate_gates(
        P.TASK_COMPLETION, facts.model_copy(update={"escalation_remaining": False})
    )
    assert set(no_esc.allowed_outcomes) == {O.ASK_HUMAN, O.REJECT}


def test_accept_stays_possible_on_the_last_attempt_when_verification_passed() -> None:
    facts = GateFacts(report=REPORTS["passed"], attempt=3, max_attempts=3)
    assert O.ACCEPT in evaluate_gates(P.TASK_COMPLETION, facts).allowed_outcomes


def test_approval_required_action_forces_ask_human_for_any_point() -> None:
    for point in P:
        facts = GateFacts(
            actions=["push"], approval_required_for=["push", "deploy"], attempt=1, max_attempts=3
        )
        g = evaluate_gates(point, facts)
        if O.ASK_HUMAN in BASE_OUTCOMES[point]:
            assert g.forced_outcome is O.ASK_HUMAN, point
            assert "gate:approval_required:push" in g.reason_codes
    other = evaluate_gates(
        P.TOOL_RISK,
        GateFacts(actions=["read"], approval_required_for=["push"], attempt=1, max_attempts=3),
    )
    assert other.forced_outcome is None


def test_budget_exhaustion_allows_only_stop_or_ask_human() -> None:
    for rep in REPORTS:
        allowed, _, reasons = gate(rep=rep, budget_exhausted=True)
        assert set(allowed) == {O.STOP, O.ASK_HUMAN}
        assert "gate:budget_exhausted" in reasons


def test_budget_point_is_always_stop_or_ask_human() -> None:
    assert set(gate(P.BUDGET)[0]) == {O.STOP, O.ASK_HUMAN}


def test_points_without_a_report_only_apply_their_own_gates() -> None:
    assert set(gate(P.PLAN_REVIEW, "none")[0]) == BASE_OUTCOMES[P.PLAN_REVIEW]
    assert set(gate(P.ROUTING, "none")[0]) == {O.CHOOSE}
    assert set(gate(P.RUN_COMPLETION, "none")[0]) == {O.ACCEPT, O.REJECT}


def test_exhaustive_invariants() -> None:
    for point, rep, exhausted, over, actions, budget, esc in itertools.product(
        list(P),
        list(REPORTS),
        [False, True],
        [False, True],
        [[], ["deploy"]],
        [False, True],
        [False, True],
    ):
        report_ = REPORTS[rep]
        facts = GateFacts(
            report=report_, attempt=3 if exhausted else 1, max_attempts=3,
            actions=actions, approval_required_for=["deploy"] if over else [],
            budget_exhausted=budget, escalation_remaining=esc,
        )  # fmt: skip
        g = evaluate_gates(point, facts)  # also validates forced ⊆ allowed
        assert g.allowed_outcomes, (point, rep)
        assert len(set(g.allowed_outcomes)) == len(g.allowed_outcomes)
        if point is P.TASK_COMPLETION and report_ and report_.overall in ("failed", "incomplete"):
            assert O.ACCEPT not in g.allowed_outcomes
        if budget:
            assert set(g.allowed_outcomes) <= {O.STOP, O.ASK_HUMAN}
        if over and actions == ["deploy"] and O.ASK_HUMAN in g.allowed_outcomes:
            assert g.forced_outcome is O.ASK_HUMAN
        assert (g.forced_outcome is None) or g.forced_outcome in g.allowed_outcomes
