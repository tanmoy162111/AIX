from __future__ import annotations

from itertools import pairwise

import pytest

import factories as f
from aix.core.failure import Classified
from aix.core.retry import (
    IdenticalRetry,
    RetryFingerprint,
    RetryLedger,
    next_step,
    remaining_mutations,
    sequence_for,
    split_task,
)
from aix.domain.enums import FailureClass as F
from aix.domain.enums import RetryMutation as M
from aix.domain.enums import VerificationFailureKind as V
from aix.domain.errors import PolicyViolation
from aix.domain.ids import IdPrefix, new_id

VF = F.VERIFICATION_FAILURE
CODE = [M.SAME_AGENT_WITH_FAILURE_CONTEXT, M.SWITCH_AGENT, M.ADD_RESEARCH_STEP, M.ASK_HUMAN]


@pytest.mark.parametrize(
    ("cls", "expected"),
    [
        (Classified(VF, V.TESTS), CODE),
        (Classified(VF, V.BUILD), CODE),
        (Classified(VF, V.TYPECHECK), CODE),
        (Classified(VF, V.LINT), CODE),
        (Classified(VF, V.SECURITY), [M.SAME_AGENT_WITH_FINDINGS, M.SWITCH_AGENT, M.ASK_HUMAN]),
        (Classified(VF, V.REVIEW), [M.SAME_AGENT_WITH_FINDINGS, M.SWITCH_AGENT, M.ASK_HUMAN]),
        (Classified(F.AGENT_NO_CHANGES), [M.SAME_AGENT_CLARIFIED_PROMPT, M.SWITCH_AGENT]),
        (Classified(F.TIMEOUT), [M.SPLIT_TASK, M.SWITCH_AGENT]),
        (Classified(F.RATE_LIMITED), [M.WAIT_AND_RETRY, M.WAIT_AND_RETRY, M.SWITCH_AGENT]),
        (Classified(F.NETWORK_FAILURE), [M.WAIT_AND_RETRY, M.WAIT_AND_RETRY, M.SWITCH_AGENT]),
        (Classified(F.AUTH_FAILURE), [M.SWITCH_AGENT]),
        (Classified(F.MERGE_CONFLICT), [M.REBASE_AND_RETRY]),
        (Classified(F.SCOPE_VIOLATION), [M.SAME_AGENT_WITH_SCOPE_REMINDER, M.SWITCH_AGENT]),
        (Classified(F.CONTEXT_FAILURE), [M.COMPACT_CONTEXT_AND_RETRY]),
        (Classified(F.POLICY_FAILURE), []),
        (Classified(F.BUDGET_EXCEEDED), []),
        (Classified(F.HUMAN_REJECTION), []),
        (Classified(F.NO_ELIGIBLE_AGENT), []),
    ],
)  # fmt: skip
def test_default_sequences_follow_the_spec_table(cls: Classified, expected: list[M]) -> None:
    assert [s.mutation for s in sequence_for(cls)] == expected


def test_every_failure_class_has_a_sequence_or_is_explicitly_terminal() -> None:
    for failure in F:
        sequence_for(Classified(failure))  # never raises


def test_next_step_walks_the_sequence_and_ends() -> None:
    cls = Classified(VF, V.TESTS)
    used: list[M] = []
    seen: list[M] = []
    while (step := next_step(cls, used)) is not None:
        seen.append(step.mutation)
        used.append(step.mutation)
    assert seen == [
        M.SAME_AGENT_WITH_FAILURE_CONTEXT,
        M.SWITCH_AGENT,
        M.ADD_RESEARCH_STEP,
        M.ASK_HUMAN,
    ]
    assert next_step(Classified(F.POLICY_FAILURE), []) is None


def test_backoff_and_non_consuming_flags() -> None:
    cls = Classified(F.RATE_LIMITED)
    first, second, third = (
        next_step(cls, []),
        next_step(cls, [M.WAIT_AND_RETRY]),
        next_step(cls, [M.WAIT_AND_RETRY] * 2),
    )
    assert first and second and third
    assert (first.wait_s, second.wait_s) == (30.0, 120.0)
    assert not first.consumes_attempt and not second.consumes_attempt and third.consumes_attempt
    auth = next_step(Classified(F.AUTH_FAILURE), [])
    assert auth and auth.mark_agent_unavailable and not auth.consumes_attempt
    normal = next_step(Classified(VF, V.TESTS), [])
    assert (
        normal
        and normal.consumes_attempt
        and not normal.mark_agent_unavailable
        and normal.wait_s == 0
    )


def test_remaining_mutations_lists_what_is_left() -> None:
    cls = Classified(VF, V.TESTS)
    assert remaining_mutations(cls, [M.SAME_AGENT_WITH_FAILURE_CONTEXT]) == [
        "switch_agent", "add_research_step", "ask_human",
    ]  # fmt: skip
    assert remaining_mutations(Classified(F.POLICY_FAILURE), []) == []


def test_identical_retries_are_forbidden() -> None:
    fp = RetryFingerprint(agent_id="a", prompt_hash="h1", base_commit="c1")
    ledger = RetryLedger()
    ledger.record(fp)
    with pytest.raises(IdenticalRetry) as exc:
        ledger.record(RetryFingerprint(agent_id="a", prompt_hash="h1", base_commit="c1"))
    assert isinstance(exc.value, PolicyViolation)
    for other in (
        RetryFingerprint(agent_id="b", prompt_hash="h1", base_commit="c1"),
        RetryFingerprint(agent_id="a", prompt_hash="h2", base_commit="c1"),
        RetryFingerprint(agent_id="a", prompt_hash="h1", base_commit="c2"),
    ):
        ledger.record(other)
    assert len(ledger) == 4


def test_fingerprint_hashes_the_prompt() -> None:
    a = RetryFingerprint.of("agent", "the prompt", "commit")
    assert a == RetryFingerprint.of("agent", "the prompt", "commit")
    assert a != RetryFingerprint.of("agent", "the prompt!", "commit")
    assert "the prompt" not in a.prompt_hash


def test_split_task_makes_at_most_three_chained_subtasks() -> None:
    run = new_id(IdPrefix.RUN)
    dep = f.task(run, title="dep")
    orig = f.task(
        run, title="big", goal="Do everything", depends_on=[dep.id], file_scope=["src/**"]
    )
    dependent = f.task(run, title="after", depends_on=[orig.id])
    subs, rewired = split_task(orig, [dependent], parts=3)
    assert 2 <= len(subs) <= 3
    assert subs[0].depends_on == [dep.id]
    for prev, nxt in pairwise(subs):
        assert nxt.depends_on == [prev.id]
    assert all(s.file_scope == ["src/**"] and s.run_id == run for s in subs)
    assert all(
        orig.goal in s.goal and f"part {i + 1}" in s.goal.lower() for i, s in enumerate(subs)
    )
    assert [t.depends_on for t in rewired] == [[subs[-1].id]]
    assert len({s.id for s in subs}) == len(subs) and orig.id not in {s.id for s in subs}
    assert subs[-1].verification == orig.verification and not subs[0].verification.required


def test_split_task_rejects_bad_part_counts() -> None:
    t = f.task(new_id(IdPrefix.RUN))
    with pytest.raises(ValueError):
        split_task(t, [], parts=1)
    with pytest.raises(ValueError):
        split_task(t, [], parts=4)
