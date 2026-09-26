"""agent_stats projection, `aix stats agents` and router input (M7.6, PLAYBOOK §22)."""

from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from aix.cli.main import app
from aix.core.router.stats import router_stats
from aix.domain.enums import TaskType
from run_env import finished_run

pytestmark = pytest.mark.anyio
runner = CliRunner()

USAGE: dict[str, object] = {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02}


async def test_projection_counts_a_finished_run(tmp_path: Path) -> None:
    _, _, store = await finished_run(tmp_path, usage=USAGE)
    try:
        (row,) = await store.agent_stats()
        assert (row.agent_id, row.task_type) == ("fake-a", "implement")
        assert row.attempts == 1 and row.accepted == 1 and row.verification_passed == 1
        assert row.retries == 0 and row.human_interventions == 0
        assert row.mean_cost_usd == pytest.approx(0.02)
        assert row.p50_latency_s is not None and row.p90_latency_s is not None
        assert row.accept_rate == 1.0
        stats = router_stats(await store.agent_stats(by_model=False))
        assert stats[("fake-a", TaskType.IMPLEMENT)].accepted == 1
    finally:
        await store.close()


async def test_rebuilding_projections_reproduces_the_stats(tmp_path: Path) -> None:
    _, _, store = await finished_run(tmp_path, usage=USAGE)
    try:
        before = await store.agent_stats()
        await store.rebuild_projections()
        assert await store.agent_stats() == before
    finally:
        await store.close()


async def _prepared(tmp_path: Path) -> Path:
    repo, _, store = await finished_run(tmp_path, usage=USAGE)
    await store.close()
    return repo


def test_cli_prints_a_table_and_json(tmp_path: Path) -> None:
    repo = anyio.run(_prepared, tmp_path)
    res = runner.invoke(app, ["stats", "agents", "--json", "--project", str(repo)])
    assert res.exit_code == 0, res.output
    (row,) = json.loads(res.output)
    assert row["agent_id"] == "fake-a" and row["accept_rate"] == 1.0
    table = runner.invoke(app, ["stats", "agents", "--project", str(repo)])
    assert (
        table.exit_code == 0 and "fake-a" in table.output and "no overall ranking" in table.output
    )


def test_router_stats_skips_unknown_task_types() -> None:
    from aix.domain.stats import AgentStatsRow

    rows = [
        AgentStatsRow(agent_id="a", model="", task_type="implement", attempts=3, accepted=2,
                      verification_passed=2, retries=0, human_interventions=0),
        AgentStatsRow(agent_id="a", model="", task_type="from_the_future", attempts=1, accepted=1,
                      verification_passed=1, retries=0, human_interventions=0),
    ]  # fmt: skip
    assert list(router_stats(rows)) == [("a", TaskType.IMPLEMENT)]
