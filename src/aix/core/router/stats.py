"""Turn recorded agent statistics into router input (PLAYBOOK §12, §22)."""

from __future__ import annotations

from collections.abc import Sequence

from aix.core.router.rules import AgentStat
from aix.domain.enums import TaskType
from aix.domain.stats import AgentStatsRow


def router_stats(rows: Sequence[AgentStatsRow]) -> dict[tuple[str, TaskType], AgentStat]:
    """``(agent, task type) -> AgentStat`` from rows merged across models.

    Rows for task types this version does not know are skipped rather than failing a run.
    """
    out: dict[tuple[str, TaskType], AgentStat] = {}
    for r in rows:
        try:
            task_type = TaskType(r.task_type)
        except ValueError:
            continue
        out[(r.agent_id, task_type)] = AgentStat(
            attempts=r.attempts,
            accepted=r.accepted,
            mean_cost_usd=r.mean_cost_usd,
            p50_latency_s=r.p50_latency_s,
        )
    return out
