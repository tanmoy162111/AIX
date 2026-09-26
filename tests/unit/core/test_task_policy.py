from __future__ import annotations

from aix.core.escalation import Step as E
from aix.core.failure import Classified
from aix.core.orchestrator.task_policy import TaskPolicy
from aix.domain.agents import AgentSpec, AgentSupports
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import FailureClass as F
from aix.domain.enums import RetryMutation as M
from aix.domain.enums import VerificationFailureKind as V
from aix.domain.state import TaskEvent as TE

TESTS = Classified(F.VERIFICATION_FAILURE, V.TESTS)
LADDER = [E.STRONGER_MODEL, E.DIFFERENT_AGENT, E.MULTI_AGENT, E.HUMAN]


def spec(i: str, models: list[str] | None = None, cur: str | None = None) -> AgentSpec:
    return AgentSpec(id=i, name=i, kind="local", health="ready", models=models or [],
                     default_model=cur, supports=AgentSupports(model_select=True))  # fmt: skip


A, B = spec("a", ["small", "big"], "small"), spec("b")


def policy(max_attempts: int = 3) -> TaskPolicy:
    return TaskPolicy(max_attempts=max_attempts, ladder=LADDER)


def plan(
    p: TaskPolicy, outcome: O, cls: Classified | None = TESTS, agent: AgentSpec = A, model=None
):  # type: ignore[no-untyped-def]
    return p.plan_next(
        outcome,
        cls,
        current=agent,
        current_model=model,
        candidates=[A, B],
        detail="1 of 3 tests failed",
    )


def test_accept_and_terminal_outcomes() -> None:
    p = policy()
    assert plan(p, O.ACCEPT, None).kind == "accept"
    rej = plan(p, O.REJECT)
    assert rej.kind == "fail" and rej.event is TE.REJECT
    assert plan(p, O.STOP).event is TE.STOP
    ask = plan(p, O.ASK_HUMAN)
    assert ask.kind == "ask_human" and ask.event is TE.ASK_HUMAN


def test_retry_walks_the_mutation_sequence_for_the_class() -> None:
    p = policy()
    first = plan(p, O.RETRY)
    assert first.kind == "retry" and first.event is TE.RETRY
    assert first.mutation is M.SAME_AGENT_WITH_FAILURE_CONTEXT and first.exclude_agent is None
    assert "1 of 3 tests failed" in "\n".join(first.notes)
    second = plan(p, O.RETRY)
    assert (
        second.mutation is M.SWITCH_AGENT
        and second.exclude_agent == "a"
        and second.event is TE.SWITCH_AGENT
    )
    third = plan(p, O.RETRY)
    assert third.mutation is M.ADD_RESEARCH_STEP
    fourth = plan(p, O.RETRY)
    assert fourth.kind == "ask_human"  # the sequence ends with ask_human


def test_switch_agent_outcome_forces_a_switch() -> None:
    p = policy()
    act = plan(p, O.SWITCH_AGENT)
    assert act.mutation is M.SWITCH_AGENT and act.exclude_agent == "a"


def test_every_retry_has_a_distinct_note_so_prompts_differ() -> None:
    p = policy()
    notes = [tuple(plan(p, O.RETRY).notes) for _ in range(3)]
    assert len(set(notes)) == 3


def test_escalate_climbs_the_ladder() -> None:
    p = policy()
    s1 = plan(p, O.ESCALATE, model="small")
    assert s1.event is TE.ESCALATE and s1.force_agent == "a" and s1.model == "big"
    s2 = plan(p, O.ESCALATE, model="big")
    assert s2.force_agent == "b" and s2.model is None
    s3 = plan(p, O.ESCALATE)
    assert s3.require_review and "independent reviewer" in " ".join(s3.notes).lower()
    s4 = plan(p, O.ESCALATE)
    assert s4.kind == "ask_human"
    assert plan(p, O.ESCALATE).kind == "ask_human"  # ladder used up


def test_escalation_remaining_tracks_the_ladder() -> None:
    p = policy()
    assert p.escalation_remaining is True
    for _ in range(4):
        plan(p, O.ESCALATE)
    assert p.escalation_remaining is False


def test_non_consuming_retries_refund_the_attempt() -> None:
    p = policy(max_attempts=3)
    p.begin_attempt()
    p.begin_attempt()
    assert p.consumed == 2
    act = plan(p, O.RETRY, Classified(F.RATE_LIMITED))
    assert act.wait_s == 30.0 and act.consumes_attempt is False and p.consumed == 1
    act2 = plan(p, O.RETRY, Classified(F.RATE_LIMITED))
    assert act2.wait_s == 120.0 and p.consumed == 0


def test_auth_failure_marks_the_agent_unavailable_without_consuming() -> None:
    p = policy()
    p.begin_attempt()
    act = plan(p, O.RETRY, Classified(F.AUTH_FAILURE))
    assert act.mark_unavailable == "a" and act.mutation is M.SWITCH_AGENT and p.consumed == 0


def test_classes_track_their_own_sequences() -> None:
    p = policy()
    assert plan(p, O.RETRY).mutation is M.SAME_AGENT_WITH_FAILURE_CONTEXT
    lint_like = plan(p, O.RETRY, Classified(F.SCOPE_VIOLATION))
    assert lint_like.mutation is M.SAME_AGENT_WITH_SCOPE_REMINDER  # fresh sequence for a new class


def test_timeout_asks_for_a_split_then_a_switch() -> None:
    p = policy()
    assert plan(p, O.RETRY, Classified(F.TIMEOUT)).split is True
    assert plan(p, O.RETRY, Classified(F.TIMEOUT)).mutation is M.SWITCH_AGENT


def test_exhausted_sequence_escalates_before_asking_a_human() -> None:
    p = policy()
    for _ in range(2):
        plan(p, O.RETRY, Classified(F.SCOPE_VIOLATION))
    act = plan(p, O.RETRY, Classified(F.SCOPE_VIOLATION))
    assert act.event is TE.ESCALATE and act.model == "big"


def test_history_and_switch_flag_feed_decision_state() -> None:
    p = policy()
    assert p.previous_failures == [] and p.agent_switched is False
    plan(p, O.RETRY)
    plan(p, O.RETRY)  # switch
    assert p.agent_switched is True and p.previous_failures == [TESTS, TESTS]
    assert p.failed_agents == set()  # recorded by the executor with the agent id


def test_no_classification_and_retry_asks_a_human() -> None:
    assert plan(policy(), O.RETRY, None).kind == "ask_human"
