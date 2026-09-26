from __future__ import annotations

from aix.store import events
from aix.store.events import EVENT_PAYLOADS, Payload

SPEC_8_3 = {
    "run.created", "run.planned", "run.state_changed", "run.completed", "run.failed",
    "run.cancelled", "task.created", "task.state_changed", "attempt.created",
    "attempt.started", "attempt.finished", "agent.selected", "agent.output",
    "agent.tool_called", "agent.failed", "workspace.created", "workspace.merged",
    "workspace.conflict", "workspace.removed", "check.started", "check.finished",
    "verification.completed", "decision.requested", "decision.completed",
    "approval.requested", "approval.granted", "approval.denied", "approval.expired",
    "policy.violation", "artifact.created", "budget.exceeded", "context.compacted",
}  # fmt: skip


def test_registry_covers_exactly_the_spec_event_types() -> None:
    assert set(EVENT_PAYLOADS) == SPEC_8_3


def test_every_payload_is_a_frozen_strict_model() -> None:
    for name, cls in EVENT_PAYLOADS.items():
        assert issubclass(cls, Payload), name
        assert cls.model_config.get("frozen") is True
        assert cls.model_config.get("extra") == "forbid"


def test_payload_class_names_end_with_payload() -> None:
    assert all(c.__name__.endswith("Payload") for c in EVENT_PAYLOADS.values())


def test_payload_types_are_unique_per_event_type() -> None:
    assert len(set(EVENT_PAYLOADS.values())) == len(EVENT_PAYLOADS)


def test_schema_version_is_one() -> None:
    assert events.SCHEMA_VERSION == 1
