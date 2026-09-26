"""Estimated cost feeds the budget and the report (M7.5, PLAYBOOK §22)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aix.artifacts.report import build_report
from aix.config.schema import AgentOverride, ModelPrice, PricingConfig
from aix.domain.enums import RunStatus
from run_env import finished_run
from verif_env import fast_config

pytestmark = pytest.mark.anyio


def priced_config(max_cost: float):  # type: ignore[no-untyped-def]
    cfg = fast_config()
    return cfg.model_copy(
        update={
            "agents": cfg.agents.model_copy(
                update={"overrides": {"fake-a": AgentOverride(model="m-x")}}
            ),
            "pricing": PricingConfig(
                models={"m-x": ModelPrice(input_per_mtok=10, output_per_mtok=0)}
            ),
            "budget": cfg.budget.model_copy(update={"max_cost_usd_per_run": max_cost}),
        }
    )


TOKENS_ONLY: dict[str, object] = {
    "input_tokens": 1_000_000,
    "output_tokens": 0,
}  # no cost_usd: must be estimated


async def test_estimated_cost_is_recorded_and_reported(tmp_path: Path) -> None:
    _, run_id, store = await finished_run(tmp_path, cfg=priced_config(100.0), usage=TOKENS_ONLY)
    try:
        report = await build_report(store, run_id)
        assert report.cost_usd == 10.0 and report.cost_estimated_usd == 10.0
    finally:
        await store.close()


async def test_estimated_cost_enforces_the_budget(tmp_path: Path) -> None:
    _, run_id, store = await finished_run(
        tmp_path, cfg=priced_config(0.01), usage=TOKENS_ONLY, expect=RunStatus.FAILED
    )
    try:
        exceeded = await store.events(run_id=run_id, types=["budget.exceeded"])
        assert len(exceeded) == 1
    finally:
        await store.close()


async def test_unpriced_usage_stays_unknown(tmp_path: Path) -> None:
    _, run_id, store = await finished_run(tmp_path, usage=TOKENS_ONLY)  # no price table
    try:
        report = await build_report(store, run_id)
        assert report.cost_usd is None and report.cost_estimated_usd == 0.0
    finally:
        await store.close()
