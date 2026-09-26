from __future__ import annotations

import json

from aix.domain import schemas
from aix.domain.runs import Run


def test_collect_models_finds_the_spec_models() -> None:
    names = set(schemas.collect_domain_models())
    assert names >= {
        "run",
        "intent",
        "task",
        "task_graph",
        "agent_spec",
        "attempt",
        "execution_result",
        "check",
        "verification_report",
        "decision_record",
        "approval",
        "artifact",
    }


def test_snake_case() -> None:
    assert schemas.schema_name("TaskGraph") == "task_graph"
    assert schemas.schema_name("Run") == "run"


def test_render_is_deterministic_json_with_trailing_newline() -> None:
    a = schemas.render_schema(Run)
    assert a == schemas.render_schema(Run)
    assert a.endswith("\n")
    doc = json.loads(a)
    assert doc["title"] == "Run"
    assert list(doc) == sorted(doc)


def test_collected_models_are_only_domain_models() -> None:
    for model in schemas.collect_domain_models().values():
        assert model.__module__.startswith("aix.domain.")
