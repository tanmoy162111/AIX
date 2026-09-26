"""plan_review for high-risk intents (M5.12, §18.2, G4 setup)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.core.orchestrator.approvals import resolve_approval
from aix.core.orchestrator.executor import RunRequest, execute_run, resume_run
from aix.domain.enums import Capability, RunStatus, TaskStatus
from aix.store.db import EventStore
from verif_env import fast_config

pytestmark = pytest.mark.anyio
C = Capability
CAPS = {C.IMPLEMENT: 0.9, C.TEST: 0.9, C.DESIGN: 0.9, C.RESEARCH: 0.9, C.REVIEW: 0.9,
        C.DOCUMENT: 0.9, C.SECURITY: 0.9, C.DEBUG: 0.9}  # fmt: skip


def scripts() -> list[FakeScript]:
    def s(task_type: str, files: dict[str, str]) -> FakeScript:
        step = FakeStep(write_files=files)
        return FakeScript(match=FakeMatch(task_type=task_type), attempts=[step])

    return [
        s("implement", {"deploy.py": "x = 1\n"}),
        s("test", {"tests/test_d.py": "def test_x():\n    assert True\n"}),
        s("document", {"docs/d.md": "# d\n"}),
    ]


async def start(repo: Path, goal: str):  # type: ignore[no-untyped-def]
    cfg = fast_config()
    registry = AdapterRegistry(cfg, builtin_ids=())
    registry.register(make_fake_entry("fake-a", capabilities=CAPS, scripts=scripts()))
    (repo / ".aix").mkdir(exist_ok=True)
    store = await EventStore.open(repo / ".aix" / "aix.db")
    with anyio.fail_after(120):
        outcome = await execute_run(
            RunRequest(project_root=repo, goal=goal), registry=registry, store=store, config=cfg
        )
    return outcome, store, registry, cfg


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


async def test_high_risk_goal_waits_for_plan_approval_and_executes_nothing(repo: Path) -> None:
    outcome, store, _, _ = await start(repo, "Deploy the service to production")
    try:
        assert outcome.status is RunStatus.WAITING_APPROVAL and outcome.exit_code == 3
        assert {t.status for t in outcome.tasks} == {TaskStatus.CREATED}
        assert not await store.events(run_id=outcome.run_id, types=["attempt.created"])
        (record,) = await store.get_decisions(outcome.run_id)
        assert record.point.value == "plan_review" and record.outcome.value == "ask_human"
        (apv,) = await store.list_approvals("pending")
        assert apv.action == "plan_review" and apv.subject == outcome.run_id
        assert outcome.pending_approvals == [apv.id]
    finally:
        await store.close()


async def test_granting_the_plan_review_runs_the_plan(repo: Path) -> None:
    outcome, store, registry, cfg = await start(repo, "Deploy the service to production")
    try:
        await resolve_approval(
            store, outcome.pending_approvals[0], grant=True, actor="me", channel="api_token"
        )
        resumed = await resume_run(
            outcome.run_id, project_root=repo, registry=registry, store=store, config=cfg,
            backoff_scale=0,
        )  # fmt: skip
        assert resumed.status is RunStatus.COMPLETED, resumed
        assert {t.status for t in resumed.tasks} == {TaskStatus.COMPLETED}
    finally:
        await store.close()


async def test_denying_the_plan_review_fails_the_run(repo: Path) -> None:
    outcome, store, _, _ = await start(repo, "Deploy the service to production")
    try:
        res = await resolve_approval(
            store, outcome.pending_approvals[0], grant=False, actor="me", channel="cli_tty"
        )
        assert res.run_failed
        run = await store.get_run(outcome.run_id)
        assert run is not None and run.status is RunStatus.FAILED
        assert {t.status for t in await store.get_tasks(outcome.run_id)} == {TaskStatus.CANCELLED}
    finally:
        await store.close()


async def test_low_risk_goal_is_not_held(repo: Path) -> None:
    outcome, store, _, _ = await start(repo, "Add a retry option")
    try:
        assert outcome.status is RunStatus.COMPLETED
        decisions = await store.get_decisions(outcome.run_id)
        assert all(d.point.value != "plan_review" for d in decisions)
    finally:
        await store.close()
