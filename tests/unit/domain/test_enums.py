from __future__ import annotations

import re
from enum import StrEnum

import pytest

from aix.domain import enums

ALL_ENUMS = [
    obj
    for obj in vars(enums).values()
    if isinstance(obj, type) and issubclass(obj, StrEnum) and obj is not StrEnum
]


def test_enums_exist() -> None:
    names = {e.__name__ for e in ALL_ENUMS}
    assert names >= {
        "TaskType",
        "Capability",
        "CheckKind",
        "FailureClass",
        "RetryMutation",
        "DecisionPoint",
        "DecisionOutcome",
        "RunStatus",
        "TaskStatus",
        "AttemptStatus",
    }


@pytest.mark.parametrize("enum_cls", ALL_ENUMS, ids=lambda e: e.__name__)
def test_values_are_lowercase_snake_case(enum_cls: type[StrEnum]) -> None:
    for member in enum_cls:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", member.value), member


def test_task_types_match_spec() -> None:
    assert {t.value for t in enums.TaskType} == {
        "inspect",
        "research",
        "design",
        "implement",
        "test",
        "review",
        "security_review",
        "document",
        "integrate",
    }


def test_check_kinds_match_spec() -> None:
    assert {c.value for c in enums.CheckKind} == {
        "build",
        "tests",
        "lint",
        "typecheck",
        "security_sast",
        "secrets",
        "deps",
        "ai_review",
        "policy",
        "custom",
    }


def test_failure_classes_match_spec_19_1() -> None:
    assert {f.value for f in enums.FailureClass} == {
        "agent_failure",
        "agent_no_changes",
        "tool_failure",
        "network_failure",
        "auth_failure",
        "rate_limited",
        "timeout",
        "verification_failure",
        "scope_violation",
        "policy_failure",
        "merge_conflict",
        "context_failure",
        "resource_failure",
        "budget_exceeded",
        "no_eligible_agent",
        "interrupted",
        "human_rejection",
    }


def test_retry_mutations_cover_spec_19_2() -> None:
    assert {m.value for m in enums.RetryMutation} == {
        "same_agent_with_failure_context",
        "same_agent_with_findings",
        "same_agent_clarified_prompt",
        "same_agent_with_scope_reminder",
        "same_agent_new_context",
        "switch_agent",
        "add_research_step",
        "split_task",
        "wait_and_retry",
        "rebase_and_retry",
        "compact_context_and_retry",
        "more_verification",
        "ask_human",
    }


def test_decision_points_match_spec_18_2() -> None:
    assert {d.value for d in enums.DecisionPoint} == {
        "task_completion",
        "failure_triage",
        "routing",
        "tool_risk",
        "plan_review",
        "budget",
        "run_completion",
    }


def test_decision_outcomes_match_spec() -> None:
    assert {o.value for o in enums.DecisionOutcome} == {
        "accept",
        "retry",
        "reject",
        "escalate",
        "switch_agent",
        "ask_human",
        "stop",
        "allow",
        "deny",
        "choose",
    }


def test_terminal_status_sets() -> None:
    run_expected = {enums.RunStatus.COMPLETED, enums.RunStatus.FAILED, enums.RunStatus.CANCELLED}
    task_expected = {
        enums.TaskStatus.COMPLETED,
        enums.TaskStatus.FAILED,
        enums.TaskStatus.CANCELLED,
    }
    assert set(enums.RUN_TERMINAL) == run_expected
    assert set(enums.TASK_TERMINAL) == task_expected
