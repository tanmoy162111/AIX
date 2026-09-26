from __future__ import annotations

from typing import Any

import pytest

from aix.decision import state as S
from aix.decision.gates import BASE_OUTCOMES
from aix.decision.providers.rules import RulesProvider
from aix.decision.state import (
    BudgetFacts,
    CheckFacts,
    DecisionState,
    FailureFacts,
    HistoryFacts,
    PlanFacts,
    RunFacts,
    TaskFacts,
    ToolFacts,
    VerificationFacts,
)
from aix.domain.base import Risk
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P

pytestmark = pytest.mark.anyio
RULES = RulesProvider()
VF = "verification_failure:tests"
RL = "rate_limited"


def tc_state(overall: str, *, attempt: int = 1, max_attempts: int = 3, risk: str = "low",
             metrics: dict[str, float] | None = None, prev: list[str] | None = None,
             switched: bool = False) -> DecisionState:  # fmt: skip
    return DecisionState(
        task=TaskFacts(type="implement", risk=risk, attempt=attempt, max_attempts=max_attempts),  # type: ignore[arg-type]
        verification=VerificationFacts(
            overall=overall,
            checks=[
                CheckFacts(kind="tests", status="passed", required=True, metrics=metrics or {})
            ],
        ),
        history=HistoryFacts(previous_failures=prev or [], agent_switched=switched),
    )


def full(point: P) -> set[O]:
    return set(BASE_OUTCOMES[point])


async def decide(point: P, state: DecisionState, allowed: set[O] | None = None) -> O:
    ans = await RULES.decide(point, state, allowed if allowed is not None else full(point))
    return ans.outcome


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (tc_state("passed"), O.ACCEPT),
        (tc_state("warning", metrics={"findings_low": 3}), O.ACCEPT),
        (tc_state("warning", metrics={"pre_existing": 1}), O.ACCEPT),
        (tc_state("warning", metrics={"findings_medium": 1}), O.RETRY),  # first attempt: retry once
        (tc_state("warning", attempt=2, metrics={"findings_medium": 1}), O.ACCEPT),  # then accept
        (tc_state("failed"), O.RETRY),
        (tc_state("failed", attempt=2, prev=[VF]), O.SWITCH_AGENT),
        (
            tc_state("failed", attempt=2, prev=["verification_failure:tests"], switched=True),
            O.RETRY,
        ),
        (tc_state("incomplete"), O.RETRY),
        (tc_state("failed", attempt=3, max_attempts=3), O.ESCALATE),
    ],
)
async def test_task_completion_table(state: DecisionState, expected: O) -> None:
    assert await decide(P.TASK_COMPLETION, state) == expected


async def test_outcomes_are_always_within_the_allowed_set() -> None:
    only = {O.ASK_HUMAN, O.REJECT}
    assert await decide(P.TASK_COMPLETION, tc_state("failed"), only) in only
    assert await decide(P.TASK_COMPLETION, tc_state("passed"), only) in only
    assert await decide(P.TASK_COMPLETION, tc_state("passed"), {O.REJECT}) is O.REJECT


def fail_state(cls: str, *, attempt: int = 1, prev: list[str] | None = None,
               switched: bool = False) -> DecisionState:  # fmt: skip
    return DecisionState(
        task=TaskFacts(type="implement", risk="low", attempt=attempt, max_attempts=3),
        history=HistoryFacts(previous_failures=prev or [], agent_switched=switched),
        failure=FailureFacts.model_validate({"failure_class": cls, "candidates": [cls]}),
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (fail_state("verification_failure"), O.RETRY),
        (
            fail_state("verification_failure", attempt=2, prev=["verification_failure"]),
            O.SWITCH_AGENT,
        ),
        (fail_state("auth_failure"), O.SWITCH_AGENT),
        (fail_state("rate_limited"), O.RETRY),
        (
            fail_state("rate_limited", attempt=2, prev=["rate_limited", "rate_limited"]),
            O.SWITCH_AGENT,
        ),
        (fail_state("merge_conflict"), O.RETRY),
        (fail_state("timeout"), O.RETRY),
        (fail_state("policy_failure"), O.ASK_HUMAN),
        (fail_state("budget_exceeded"), O.ASK_HUMAN),
        (fail_state("human_rejection"), O.ASK_HUMAN),
        (fail_state("agent_no_changes"), O.RETRY),
        (fail_state("agent_no_changes", attempt=2, prev=["agent_no_changes"]), O.SWITCH_AGENT),
        (fail_state("context_failure"), O.RETRY),
    ],
)
async def test_failure_triage_table(state: DecisionState, expected: O) -> None:
    assert await decide(P.FAILURE_TRIAGE, state) == expected


def tool(classes: list[str], *, in_scope: bool = True, risk: Risk = "low") -> DecisionState:
    return DecisionState(
        tool=ToolFacts(classes=classes, target_paths=["a.py"], task_risk=risk, in_scope=in_scope)
    )


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (tool(["read_only"]), O.ALLOW),
        (tool(["local_write"]), O.ALLOW),
        (tool(["local_write"], in_scope=False), O.DENY),
        (tool(["git_push"]), O.ASK_HUMAN),
        (tool(["deploy"]), O.ASK_HUMAN),
        (tool(["local_delete"], risk="high"), O.ASK_HUMAN),
        (tool(["external_network"]), O.ASK_HUMAN),
        (tool(["mystery"]), O.ASK_HUMAN),
    ],
)
async def test_tool_risk_table(state: DecisionState, expected: O) -> None:
    assert await decide(P.TOOL_RISK, state) == expected


def plan(risk: str = "low", *, gaps: int = 0, external: list[str] | None = None) -> DecisionState:
    facts = PlanFacts(
        risk=risk,  # type: ignore[arg-type]
        tasks=5,
        write_tasks=2,
        write_tasks_without_checks=gaps,
        external_side_effect_types=external or [],
    )
    return DecisionState(plan=facts)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (plan(), O.ACCEPT),
        (plan("medium"), O.ACCEPT),
        (plan("high"), O.ASK_HUMAN),
        (plan(gaps=1), O.REJECT),
        (plan(external=["integrate"]), O.ASK_HUMAN),
    ],
)
async def test_plan_review_table(state: DecisionState, expected: O) -> None:
    assert await decide(P.PLAN_REVIEW, state) == expected


def budget(ratio: float, done: int, total: int) -> DecisionState:
    return DecisionState(
        budget=BudgetFacts(
            kind="cost_usd", used_ratio=ratio, tasks_completed=done, tasks_total=total
        )
    )


async def test_budget_table() -> None:
    assert await decide(P.BUDGET, budget(1.1, 1, 6)) is O.STOP
    assert (
        await decide(P.BUDGET, budget(1.1, 5, 6)) is O.ASK_HUMAN
    )  # nearly done: let a human decide
    assert await decide(P.BUDGET, budget(3.0, 5, 6)) is O.STOP


def run_state(**kw: Any) -> DecisionState:
    data = {"completed": 3, "failed": 0, "cancelled": 0, "blocked": 0, "risk": "low"} | kw
    return DecisionState(run=RunFacts(**data))


async def test_run_completion_table() -> None:
    assert await decide(P.RUN_COMPLETION, run_state()) is O.ACCEPT
    for bad in ({"failed": 1}, {"blocked": 1}, {"cancelled": 1}):
        assert await decide(P.RUN_COMPLETION, run_state(**bad)) is O.REJECT


async def test_routing_picks_the_first_candidate_deterministically() -> None:
    state = S.build_routing_state(
        __import__("aix.domain.enums", fromlist=["TaskType"]).TaskType.IMPLEMENT, ["b", "a"]
    )
    ans = await RULES.decide(P.ROUTING, state, {O.CHOOSE})
    assert ans.outcome is O.CHOOSE and ans.choice == "a"


async def test_answers_carry_reason_codes_and_the_provider_name() -> None:
    ans = await RULES.decide(P.TASK_COMPLETION, tc_state("passed"), full(P.TASK_COMPLETION))
    assert RULES.name == "rules" and ans.reason_codes and ans.reason_codes[0].startswith("rules:")


async def test_missing_state_sections_defer_to_ask_human() -> None:
    assert await decide(P.TASK_COMPLETION, DecisionState()) is O.ASK_HUMAN
