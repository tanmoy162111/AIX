"""Run traces derived from recorded state (PLAYBOOK §22)."""

from __future__ import annotations

from dataclasses import dataclass, field

from aix.store.db import EventStore


@dataclass
class TraceNode:
    kind: str
    """``run``, ``task``, ``attempt``, ``check`` or ``decision``."""
    id: str
    label: str
    status: str
    duration_ms: int | None = None
    children: list[TraceNode] = field(default_factory=list["TraceNode"])

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.id,
            "label": self.label,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "children": [c.to_dict() for c in self.children],
        }


async def build_trace(store: EventStore, run_id: str) -> TraceNode:
    """The trace tree of ``run_id``, in creation order. Durations are recorded ones (no clock).

    Raises:
        LookupError: unknown run.
    """
    run = await store.get_run(run_id)
    if run is None:
        raise LookupError(f"unknown run {run_id!r}")
    duration = (
        int((run.finished_at - run.created_at).total_seconds() * 1000) if run.finished_at else None
    )
    root = TraceNode("run", run.id, run.goal, run.status.value, duration)
    decisions = await store.get_decisions(run_id)
    by_subject: dict[str, list[TraceNode]] = {}
    for d in decisions:
        node = TraceNode(
            "decision",
            d.id,
            f"{d.point.value} -> {d.outcome.value} ({d.provider})",
            d.outcome.value,
        )
        by_subject.setdefault(d.subject, []).append(node)

    for task in await store.get_tasks(run_id):
        t_node = TraceNode("task", task.id, f"{task.type.value}: {task.title}", task.status.value)
        total = 0
        for attempt in await store.get_attempts(task.id):
            result = await store.get_result(attempt.id)
            label = f"attempt #{attempt.number} {attempt.agent_id}"
            if attempt.model:
                label += f"/{attempt.model}"
            if attempt.mutation:
                label += f" ({attempt.mutation.value})"
            status = attempt.status.value
            if result and result.failure:
                status += f" [{result.failure.value}]"
            a_node = TraceNode(
                "attempt", attempt.id, label, status, result.duration_ms if result else None
            )
            total += result.duration_ms if result else 0
            for c in await store.get_checks(attempt.id):
                a_node.children.append(
                    TraceNode(
                        "check", c.id, f"{c.kind.value}: {c.summary}", c.status, c.duration_ms
                    )
                )
            a_node.children.extend(by_subject.pop(attempt.id, []))
            t_node.children.append(a_node)
        t_node.duration_ms = total or None
        t_node.children.extend(by_subject.pop(task.id, []))
        root.children.append(t_node)
    for leftover in by_subject.values():
        root.children.extend(leftover)
    return root
