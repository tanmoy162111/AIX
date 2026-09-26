from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from aix.domain.ids import (
    ArtifactId,
    AttemptId,
    CheckId,
    DecisionId,
    EventId,
    ExecutionId,
    GraphId,
    IdPrefix,
    RunId,
    TaskId,
    id_prefix,
    new_id,
)


@pytest.mark.parametrize("prefix", list(IdPrefix))
def test_new_id_has_prefix_and_26_char_ulid(prefix: IdPrefix) -> None:
    value = new_id(prefix)
    head, _, tail = value.partition("_")
    assert head == prefix.value
    assert len(tail) == 26
    assert id_prefix(value) is prefix


def test_new_ids_are_unique_and_sortable_by_time() -> None:
    ids = [new_id(IdPrefix.EVENT) for _ in range(200)]
    assert len(set(ids)) == 200


def test_spec_prefixes() -> None:
    assert {p.value for p in IdPrefix} >= {
        "run",
        "task",
        "att",
        "exe",
        "chk",
        "dec",
        "apv",
        "art",
        "evt",
        "gph",
    }


class Holder(BaseModel):
    run: RunId
    task: TaskId
    attempt: AttemptId
    execution: ExecutionId
    check: CheckId
    decision: DecisionId
    artifact: ArtifactId
    event: EventId
    graph: GraphId


def _holder() -> dict[str, str]:
    return {
        "run": new_id(IdPrefix.RUN),
        "task": new_id(IdPrefix.TASK),
        "attempt": new_id(IdPrefix.ATTEMPT),
        "execution": new_id(IdPrefix.EXECUTION),
        "check": new_id(IdPrefix.CHECK),
        "decision": new_id(IdPrefix.DECISION),
        "artifact": new_id(IdPrefix.ARTIFACT),
        "event": new_id(IdPrefix.EVENT),
        "graph": new_id(IdPrefix.GRAPH),
    }


def test_pydantic_accepts_well_formed_ids() -> None:
    assert Holder.model_validate(_holder()).run.startswith("run_")


def test_pydantic_rejects_wrong_prefix() -> None:
    data = _holder()
    data["run"] = data["task"]
    with pytest.raises(ValidationError):
        Holder.model_validate(data)


@pytest.mark.parametrize("bad", ["", "run_", "run_123", "run_" + "!" * 26, "RUN_" + "0" * 26])
def test_pydantic_rejects_malformed_ids(bad: str) -> None:
    data = _holder()
    data["run"] = bad
    with pytest.raises(ValidationError):
        Holder.model_validate(data)


def test_id_prefix_rejects_unknown() -> None:
    with pytest.raises(ValueError):
        id_prefix("zzz_" + "0" * 26)
