from __future__ import annotations

import pytest

from aix.config.schema import AixConfig
from aix.decision import questions as Q
from aix.decision import state as S
from aix.decision.gates import GateFacts
from aix.decision.jev import FakeJevClient, JevAnswer, JevError
from aix.decision.providers.jev_provider import JevDecisionProvider
from aix.decision.providers.rules import RulesProvider
from aix.decision.service import DecisionService
from aix.decision.state import (
    CheckFacts,
    DecisionState,
    FailureFacts,
    PlanFacts,
    TaskFacts,
    ToolFacts,
    VerificationFacts,
)
from aix.decision.thresholds import Thresholds
from aix.domain.base import Risk
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P
from aix.domain.enums import TaskType

pytestmark = pytest.mark.anyio
TH = Thresholds(AixConfig().decision.thresholds)


def choice(c: str, conf: float) -> JevAnswer:
    return JevAnswer(kind="choice", choice=c, confidence=conf)


def score(v: float) -> JevAnswer:
    return JevAnswer(kind="score", score=v, confidence=0.9)


def noul(v: float) -> JevAnswer:
    return JevAnswer(kind="noul", noul=v)


def tc_state(overall: str = "passed", risk: Risk = "low", attempt: int = 1) -> DecisionState:
    return DecisionState(
        task=TaskFacts(type="implement", risk=risk, attempt=attempt, max_attempts=3),
        verification=VerificationFacts(
            overall=overall, checks=[CheckFacts(kind="tests", status="passed", required=True)]
        ),
    )


def provider(client: FakeJevClient) -> JevDecisionProvider:
    return JevDecisionProvider(client, RulesProvider(), TH)


ALLOWED = {O.ACCEPT, O.RETRY, O.SWITCH_AGENT, O.ESCALATE, O.ASK_HUMAN, O.REJECT}


def tc_answers(
    c: str = "accept", conf: float = 0.95, blocking: float = 0.1, risk: float = 0.5
) -> dict[str, JevAnswer]:
    return {
        "completion": choice(c, conf),
        "residual_risk": score(risk),
        "warnings_blocking": noul(blocking),
    }


# ---- catalog ---------------------------------------------------------------------------------


def test_catalog_shapes_match_appendix_a() -> None:
    tc = Q.task_completion()
    assert set(tc) == {"completion", "residual_risk", "warnings_blocking"}
    assert set(tc["completion"].criteria) == {
        "accept",
        "fix_and_retry",
        "different_agent",
        "needs_human",
    }
    assert len(tc["residual_risk"].levels) == 4 and tc["warnings_blocking"].kind == "noul"
    ft = Q.failure_triage(
        ["verification_failure", "agent_failure"],
        ["same_agent_with_failure_context", "switch_agent"],
    )
    assert set(ft["failure_class"].criteria) == {"verification_failure", "agent_failure"}
    assert set(ft["mutation"].criteria) == {"same_agent_with_failure_context", "switch_agent"}
    assert (
        set(Q.tool_risk()) == {"risk", "outside_scope"} and len(Q.tool_risk()["risk"].levels) == 4
    )
    assert set(Q.plan_review()) == {"external_side_effects", "missing_verification", "plan_risk"}
    assert set(Q.intent_classify()) == {"kind", "risk"}
    assert set(Q.route_pick_agent({"a": "x", "b": "y"})["agent"].criteria) == {"a", "b"}


def test_no_question_text_can_carry_state_or_vendor_names() -> None:
    for qs in (Q.task_completion(), Q.tool_risk(), Q.plan_review(), Q.intent_classify()):
        for q in qs.values():
            text = q.model_dump_json().lower()
            assert not any(v in text for v in ("claude", "codex", "gemini", "opencode"))


# ---- task_completion mapping (§18.5) ---------------------------------------------------------


@pytest.mark.parametrize(
    ("answers", "state", "outcome", "reason"),
    [
        (tc_answers(), tc_state(), O.ACCEPT, "jev:accept"),
        (
            tc_answers(conf=0.84),
            tc_state(),
            O.ACCEPT,
            "rules:",
        ),  # below 0.85: rules decide (passed -> accept)
        (
            tc_answers(blocking=0.4),
            tc_state("failed"),
            O.RETRY,
            "rules:",
        ),  # blocking warning: not accept
        (tc_answers(risk=1.5), tc_state("failed"), O.RETRY, "rules:"),
        (tc_answers("fix_and_retry", 0.7), tc_state("failed"), O.RETRY, "jev:fix_and_retry"),
        (
            tc_answers("different_agent", 0.7),
            tc_state("failed"),
            O.SWITCH_AGENT,
            "jev:different_agent",
        ),
        (tc_answers("needs_human", 0.7), tc_state("passed"), O.ASK_HUMAN, "jev:needs_human"),
        (tc_answers("fix_and_retry", 0.4), tc_state("passed"), O.ACCEPT, "jev:low_confidence"),
        (
            tc_answers(conf=0.9),
            tc_state("passed", risk="high"),
            O.ACCEPT,
            "rules:",
        ),  # 0.9 < 0.92 for high risk
    ],
)
async def test_task_completion_mapping(
    answers: dict[str, JevAnswer], state: DecisionState, outcome: O, reason: str
) -> None:
    client = FakeJevClient(answers)
    ans = await provider(client).decide(P.TASK_COMPLETION, state, ALLOWED)
    assert ans.outcome is outcome and any(r.startswith(reason) for r in ans.reason_codes)
    assert ans.questions and ans.answers  # both recorded even when rules decided


async def test_high_risk_needs_a_higher_accept_confidence() -> None:
    ans = await provider(FakeJevClient(tc_answers(conf=0.95))).decide(
        P.TASK_COMPLETION, tc_state(risk="high"), ALLOWED
    )
    assert "jev:accept" in ans.reason_codes
    mid = await provider(FakeJevClient(tc_answers(conf=0.9))).decide(
        P.TASK_COMPLETION, tc_state("failed", risk="high"), ALLOWED
    )
    assert "jev:accept" not in mid.reason_codes and mid.outcome is not O.ACCEPT


async def test_state_sent_to_jev_is_the_sparse_wire_state() -> None:
    client = FakeJevClient(tc_answers())
    state = tc_state()
    await provider(client).decide(P.TASK_COMPLETION, state, ALLOWED)
    assert client.calls[0].state == state.to_wire()


async def test_incomplete_answers_defer_to_rules() -> None:
    ans = await provider(FakeJevClient({})).decide(P.TASK_COMPLETION, tc_state("passed"), ALLOWED)
    assert ans.outcome is O.ACCEPT and "jev:incomplete_answer" in ans.reason_codes


async def test_points_without_a_catalog_entry_use_rules() -> None:
    client = FakeJevClient()
    st = S.build_run_completion_state(completed=2, failed=0, cancelled=0, blocked=0, risk="low")
    ans = await provider(client).decide(P.RUN_COMPLETION, st, {O.ACCEPT, O.REJECT})
    assert (
        ans.outcome is O.ACCEPT and "jev:not_applicable" in ans.reason_codes and client.calls == []
    )


# ---- failure triage / tool risk / plan / routing ---------------------------------------------


def ft_state(mutations: list[str]) -> DecisionState:
    return DecisionState(
        task=TaskFacts(type="implement", risk="low", attempt=1, max_attempts=3),
        failure=FailureFacts.model_validate(
            {
                "failure_class": "verification_failure",
                "candidates": ["verification_failure", "agent_failure"],
                "mutations": mutations,
            }
        ),
    )


async def test_failure_triage_maps_the_chosen_mutation() -> None:
    muts = ["same_agent_with_failure_context", "switch_agent", "ask_human"]
    ok = await provider(
        FakeJevClient(
            {"mutation": choice("switch_agent", 0.9), "failure_class": choice("agent_failure", 0.9)}
        )
    ).decide(
        P.FAILURE_TRIAGE,
        ft_state(muts),
        {O.RETRY, O.SWITCH_AGENT, O.ASK_HUMAN, O.REJECT, O.ESCALATE},
    )
    assert (
        ans_ok(ok, O.SWITCH_AGENT)
        and ok.meta["mutation"] == "switch_agent"
        and ok.meta["refined_class"] == "agent_failure"
    )
    low = await provider(FakeJevClient({"mutation": choice("switch_agent", 0.3)})).decide(
        P.FAILURE_TRIAGE, ft_state(muts), {O.RETRY, O.SWITCH_AGENT, O.ASK_HUMAN}
    )
    assert "jev:low_confidence" in low.reason_codes
    off = await provider(FakeJevClient({"mutation": choice("split_task", 0.9)})).decide(
        P.FAILURE_TRIAGE, ft_state(muts), {O.RETRY, O.SWITCH_AGENT}
    )
    assert "jev:mutation_not_in_sequence" in off.reason_codes


def ans_ok(a, outcome: O) -> bool:  # type: ignore[no-untyped-def]
    return a.outcome is outcome


def tool_state() -> DecisionState:
    return DecisionState(
        tool=ToolFacts(
            classes=["local_write"], target_paths=["a.py"], task_risk="low", in_scope=True
        )
    )


@pytest.mark.parametrize(
    ("risk", "outside", "outcome"),
    [(0.5, 0.0, O.ALLOW), (1.7, 0.0, O.ASK_HUMAN), (2.8, 0.0, O.DENY), (0.5, 0.5, O.DENY)],
)
async def test_tool_risk_mapping(risk: float, outside: float, outcome: O) -> None:
    ans = await provider(
        FakeJevClient({"risk": score(risk), "outside_scope": noul(outside)})
    ).decide(P.TOOL_RISK, tool_state(), {O.ALLOW, O.DENY, O.ASK_HUMAN})
    assert ans.outcome is outcome


def plan_state() -> DecisionState:
    return DecisionState(
        plan=PlanFacts(risk="high", tasks=4, write_tasks=2, write_tasks_without_checks=0)
    )


@pytest.mark.parametrize(
    ("ext", "missing", "risk", "outcome"),
    [
        (0.1, 0.1, 0.5, O.ACCEPT),
        (0.9, 0.1, 0.5, O.ASK_HUMAN),
        (0.1, 0.9, 0.5, O.REJECT),
        (0.1, 0.1, 1.8, O.ASK_HUMAN),
    ],
)
async def test_plan_review_mapping(ext: float, missing: float, risk: float, outcome: O) -> None:
    client = FakeJevClient(
        {
            "external_side_effects": noul(ext),
            "missing_verification": noul(missing),
            "plan_risk": score(risk),
        }
    )
    ans = await provider(client).decide(
        P.PLAN_REVIEW, plan_state(), {O.ACCEPT, O.ASK_HUMAN, O.REJECT}
    )
    assert ans.outcome is outcome


async def test_routing_choice_must_be_a_candidate() -> None:
    st = S.build_routing_state(TaskType.IMPLEMENT, ["a", "b"])
    good = await provider(FakeJevClient({"agent": choice("b", 0.8)})).decide(
        P.ROUTING, st, {O.CHOOSE}
    )
    assert good.outcome is O.CHOOSE and good.choice == "b"
    bad = await provider(FakeJevClient({"agent": choice("zzz", 0.9)})).decide(
        P.ROUTING, st, {O.CHOOSE}
    )
    assert bad.choice == "a"  # rules pick the first candidate
    shy = await provider(FakeJevClient({"agent": choice("b", 0.2)})).decide(
        P.ROUTING, st, {O.CHOOSE}
    )
    assert shy.choice == "a" and "jev:low_confidence" in shy.reason_codes


# ---- through the Decision Service ------------------------------------------------------------


def service(client: FakeJevClient) -> DecisionService:
    return DecisionService(provider(client), RulesProvider(), policy_version="p1", timeout_s=2)


async def test_gate_beats_jev_when_a_required_check_failed() -> None:
    from aix.domain.enums import CheckKind as K
    from aix.domain.ids import IdPrefix, new_id
    from aix.domain.verification import Check, VerificationReport, compute_overall

    bad = Check(
        id=new_id(IdPrefix.CHECK), kind=K.TESTS, status="failed", required=True, summary="s"
    )
    rep = VerificationReport(
        attempt_id=new_id(IdPrefix.ATTEMPT), checks=[bad], overall=compute_overall([bad])
    )
    st = tc_state("failed")
    rec = await service(FakeJevClient(tc_answers())).decide(
        P.TASK_COMPLETION, "t", st, GateFacts(report=rep, attempt=1, max_attempts=3)
    )
    assert rec.outcome is not O.ACCEPT and "jev:outcome_not_allowed" in rec.reason_codes
    assert rec.gate_result.reason_codes == ["gate:required_check_failed"]


async def test_jev_outage_falls_back_with_a_reason() -> None:
    rec = await service(FakeJevClient(error=JevError("down"))).decide(
        P.TASK_COMPLETION, "t", tc_state(), GateFacts(attempt=1, max_attempts=3)
    )
    assert (
        rec.provider == "rules"
        and "jev:unavailable" in rec.reason_codes
        and rec.outcome is O.ACCEPT
    )
