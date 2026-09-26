from __future__ import annotations

import pytest

from aix.core.budget import BudgetTracker
from aix.core.escalation import Escalation, EscalationState, Step, next_escalation
from aix.domain.agents import AgentSpec, AgentSupports
from aix.domain.runs import Budget

LADDER = [Step.STRONGER_MODEL, Step.DIFFERENT_AGENT, Step.MULTI_AGENT, Step.HUMAN]


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def tracker(**kw: float | int) -> tuple[BudgetTracker, Clock]:
    clock = Clock()
    b = Budget(
        max_cost_usd=float(kw.get("cost", 1.0)),
        max_attempts_total=int(kw.get("attempts", 3)),
        max_wall_seconds=int(kw.get("wall", 60)),
    )
    return BudgetTracker(b, clock=clock), clock


def test_nothing_is_exceeded_at_the_start() -> None:
    t, _ = tracker()
    assert t.exceeded() is None and t.can_start_attempt() is None


def test_cost_is_exceeded_only_strictly_above_the_limit() -> None:
    t, _ = tracker(cost=1.0)
    t.add_cost(1.0)
    assert t.exceeded() is None
    t.add_cost(0.01)
    e = t.exceeded()
    assert e and e.budget == "cost_usd" and e.limit == 1.0 and e.actual == pytest.approx(1.01)


def test_unknown_cost_is_ignored() -> None:
    t, _ = tracker(cost=1.0)
    t.add_cost(None)
    assert t.cost_usd == 0.0 and t.exceeded() is None


def test_attempt_limit_blocks_starting_another_attempt() -> None:
    t, _ = tracker(attempts=2)
    t.start_attempt()
    assert t.can_start_attempt() is None
    t.start_attempt()
    e = t.can_start_attempt()
    assert e and e.budget == "attempts" and e.actual == 2 and e.limit == 2
    assert t.exceeded() is None  # having used exactly the limit is not an overrun


def test_wall_clock_limit() -> None:
    t, clock = tracker(wall=60)
    clock.t += 60
    assert t.exceeded() is None
    clock.t += 1
    e = t.exceeded()
    assert e and e.budget == "wall_seconds" and e.actual == pytest.approx(61)


def test_used_ratio_and_multiple_overruns_report_cost_first() -> None:
    t, clock = tracker(cost=1.0, wall=10)
    t.add_cost(2.0)
    clock.t += 100
    e = t.exceeded()
    assert e and e.budget == "cost_usd"
    assert t.used_ratio("cost_usd") == 2.0 and t.used_ratio("wall_seconds") == pytest.approx(10.0)


# ---- escalation ------------------------------------------------------------------------------


def spec(agent_id: str, *, models: list[str] | None = None, current: str | None = None,
         model_select: bool = True, health: str = "ready") -> AgentSpec:  # fmt: skip
    return AgentSpec(
        id=agent_id, name=agent_id, kind="local", models=models or [], default_model=current,
        supports=AgentSupports(model_select=model_select), health=health,  # type: ignore[arg-type]
    )  # fmt: skip


def esc(
    state: EscalationState, current: AgentSpec, others: list[AgentSpec], model: str | None = None
) -> Escalation | None:
    return next_escalation(
        LADDER,
        state,
        current=current,
        current_model=model or current.default_model,
        candidates=others,
    )


def test_ladder_walks_stronger_model_then_agent_then_pair_then_human() -> None:
    a = spec("a", models=["small", "big"], current="small")
    b = spec("b")
    state = EscalationState()
    first = esc(state, a, [a, b])
    assert (
        first
        and first.step is Step.STRONGER_MODEL
        and first.agent_id == "a"
        and first.model == "big"
    )
    second = esc(state, a, [a, b], model="big")
    assert second and second.step is Step.DIFFERENT_AGENT and second.agent_id == "b"
    third = esc(state, a, [a, b])
    assert third and third.step is Step.MULTI_AGENT and third.require_review
    fourth = esc(state, a, [a, b])
    assert fourth and fourth.step is Step.HUMAN
    assert esc(state, a, [a, b]) is None and not state.remaining(LADDER)


def test_inapplicable_steps_are_skipped() -> None:
    no_select = spec("a", models=["small", "big"], current="small", model_select=False)
    only = EscalationState()
    got = esc(only, no_select, [no_select])
    assert got and got.step is Step.HUMAN  # no stronger model, no other agent, no reviewer pair
    top = spec("a", models=["small", "big"], current="big")
    assert (r := esc(EscalationState(), top, [top, spec("b")])) and r.step is Step.DIFFERENT_AGENT


def test_unavailable_agents_are_not_escalation_targets() -> None:
    a, down = spec("a"), spec("down", health="unavailable")
    got = esc(EscalationState(), a, [a, down])
    assert got and got.step is Step.HUMAN


def test_remaining_reflects_untaken_steps_and_custom_ladders() -> None:
    state = EscalationState()
    assert state.remaining(LADDER) is True
    only_human = [Step.HUMAN]
    a = spec("a")
    got = next_escalation(only_human, state, current=a, current_model=None, candidates=[a])
    assert got and got.step is Step.HUMAN and state.remaining(only_human) is False
