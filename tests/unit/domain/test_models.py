from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

import factories as f
from aix.domain.enums import (
    AttemptStatus,
    CheckKind,
    DecisionOutcome,
    RunStatus,
    TaskStatus,
)
from aix.domain.execution import Usage
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import TaskGraph
from aix.domain.verification import compute_overall

RID = new_id(IdPrefix.RUN)


def _all_models() -> list[BaseModel]:
    t = f.task(RID)
    a = f.attempt(t.id)
    return [
        f.intent(),
        t,
        f.graph(RID, [t]),
        f.run(),
        f.agent_spec(),
        a,
        f.execution_result(a.id),
        f.check(),
        f.report(a.id, [f.check()], "passed"),
        f.decision(),
        f.approval(),
        f.artifact(RID),
    ]


@pytest.mark.parametrize("model", _all_models(), ids=lambda m: type(m).__name__)
def test_json_round_trip_is_lossless(model: BaseModel) -> None:
    again = type(model).model_validate_json(model.model_dump_json())
    assert again == model


@pytest.mark.parametrize("model", _all_models(), ids=lambda m: type(m).__name__)
def test_models_are_frozen_and_forbid_extras(model: BaseModel) -> None:
    field = next(iter(type(model).model_fields))
    with pytest.raises(ValidationError):
        setattr(model, field, getattr(model, field))
    data: dict[str, Any] = model.model_dump()
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        type(model).model_validate(data)


# ---- TaskGraph invariants -------------------------------------------------


def test_graph_requires_at_least_one_task() -> None:
    with pytest.raises(ValidationError, match="at least one"):
        f.graph(RID, [])


def test_graph_rejects_duplicate_ids() -> None:
    t = f.task(RID)
    with pytest.raises(ValidationError, match="unique"):
        f.graph(RID, [t, t])


def test_graph_rejects_missing_dependency() -> None:
    t = f.task(RID, depends_on=[new_id(IdPrefix.TASK)])
    with pytest.raises(ValidationError, match="unknown dependency"):
        f.graph(RID, [t])


def test_graph_rejects_cycles() -> None:
    a_id, b_id = new_id(IdPrefix.TASK), new_id(IdPrefix.TASK)
    a = f.task(RID, id=a_id, depends_on=[b_id])
    b = f.task(RID, id=b_id, depends_on=[a_id])
    with pytest.raises(ValidationError, match="cycle"):
        f.graph(RID, [a, b])


def test_task_cannot_depend_on_itself() -> None:
    tid = new_id(IdPrefix.TASK)
    with pytest.raises(ValidationError, match="itself"):
        f.task(RID, id=tid, depends_on=[tid])


def test_graph_rejects_tasks_from_another_run() -> None:
    other = f.task(new_id(IdPrefix.RUN))
    with pytest.raises(ValidationError, match="run_id"):
        f.graph(RID, [other])


def test_topological_order_puts_dependencies_first() -> None:
    a = f.task(RID)
    b = f.task(RID, depends_on=[a.id])
    c = f.task(RID, depends_on=[b.id, a.id])
    g: TaskGraph = f.graph(RID, [c, b, a])
    order = [t.id for t in g.topological_order()]
    assert order.index(a.id) < order.index(b.id) < order.index(c.id)


def test_max_attempts_bounds() -> None:
    with pytest.raises(ValidationError):
        f.task(RID, max_attempts=0)
    with pytest.raises(ValidationError):
        f.task(RID, max_attempts=7)
    assert f.task(RID, max_attempts=6).max_attempts == 6


# ---- other validators -----------------------------------------------------


def test_agent_capability_priors_are_unit_interval() -> None:
    with pytest.raises(ValidationError):
        f.agent_spec(capabilities={"implement": 1.5})


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValidationError):
        f.run(created_at=datetime(2026, 1, 1))


def test_run_finished_at_requires_terminal_status() -> None:
    with pytest.raises(ValidationError, match="terminal"):
        f.run(status=RunStatus.EXECUTING, finished_at=f.NOW)
    assert f.run(status=RunStatus.COMPLETED, finished_at=f.NOW).finished_at == f.NOW


def test_ids_are_validated_inside_models() -> None:
    with pytest.raises(ValidationError):
        f.attempt("not-an-id")


def test_decision_choose_requires_choice_and_vice_versa() -> None:
    with pytest.raises(ValidationError, match="choice"):
        f.decision(outcome=DecisionOutcome.CHOOSE, choice=None)
    with pytest.raises(ValidationError, match="choice"):
        f.decision(outcome=DecisionOutcome.ACCEPT, choice="codex")
    ok = f.decision(outcome=DecisionOutcome.CHOOSE, choice="codex")
    assert ok.choice == "codex"


def test_decision_inputs_hash_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        f.decision(inputs_hash="xyz")


def test_gate_forced_outcome_must_be_allowed() -> None:
    from aix.domain.decisions import GateResult

    with pytest.raises(ValidationError):
        GateResult(forced_outcome=DecisionOutcome.ACCEPT, allowed_outcomes=[DecisionOutcome.STOP])


def test_approval_decided_states_need_actor_channel_and_time() -> None:
    with pytest.raises(ValidationError, match="actor"):
        f.approval(status="granted")
    ok = f.approval(status="granted", actor="tanmoy", channel="cli_tty", decided_at=f.NOW)
    assert ok.status == "granted"


def test_artifact_sha256_and_size() -> None:
    with pytest.raises(ValidationError):
        f.artifact(RID, sha256="short")
    with pytest.raises(ValidationError):
        f.artifact(RID, size=-1)


def test_usage_rejects_negative_numbers() -> None:
    with pytest.raises(ValidationError):
        Usage(input_tokens=-1)


def test_attempt_number_is_positive() -> None:
    with pytest.raises(ValidationError):
        f.attempt(new_id(IdPrefix.TASK), number=0)
    assert f.attempt(new_id(IdPrefix.TASK)).status is AttemptStatus.CREATED


# ---- VerificationReport.overall (§6) ---------------------------------------


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([], "passed"),
        ([dict()], "passed"),
        ([dict(status="failed")], "failed"),
        ([dict(status="error")], "failed"),
        ([dict(status="skipped")], "incomplete"),
        ([dict(status="failed"), dict(status="skipped")], "failed"),
        ([dict(status="warning")], "warning"),
        ([dict(required=False, status="failed")], "warning"),
        ([dict(required=False, status="error")], "warning"),
        ([dict(required=False, status="skipped")], "passed"),
        ([dict(required=False, status="warning")], "warning"),
        ([dict(), dict(kind=CheckKind.LINT, status="warning")], "warning"),
    ],
)
def test_compute_overall_rule(checks: list[dict[str, Any]], expected: str) -> None:
    assert compute_overall([f.check(**c) for c in checks]) == expected


def test_report_overall_must_match_rule() -> None:
    aid = new_id(IdPrefix.ATTEMPT)
    with pytest.raises(ValidationError, match="overall"):
        f.report(aid, [f.check(status="failed")], "passed")
    assert f.report(aid, [f.check(status="failed")], "failed").overall == "failed"


def test_task_status_default_is_created() -> None:
    assert f.task(RID).status is TaskStatus.CREATED
