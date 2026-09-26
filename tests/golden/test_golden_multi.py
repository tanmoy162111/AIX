"""Golden scenarios G2 (multi-agent) and G5 (unavailable primary), PLAYBOOK §28.

Verification uses fast no-op commands (tests/verif_env.py); G1/G6/G7 use real checks.
"""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.core.orchestrator.executor import RunOutcome, RunRequest, execute_run
from aix.domain.enums import Capability, RunStatus, TaskStatus
from aix.store.db import EventStore
from verif_env import fast_config

pytestmark = pytest.mark.anyio
C = Capability
SCRIPTS = repos.FIXTURES / "agent_scripts"


def script(task_type: str, **step: object) -> FakeScript:
    return FakeScript(
        match=FakeMatch(task_type=task_type), attempts=[FakeStep.model_validate(step)]
    )


def writer_scripts(implement: Mapping[str, object]) -> list[FakeScript]:
    return [
        script("implement", **implement),
        script("test", write_files={"tests/test_extra.py": "def test_extra():\n    assert True\n"}),
        script("document", write_files={"docs/notes.md": "# Notes\n"}),
    ]


async def go(
    repo: Path, agents: list[tuple[str, dict[str, object]]], goal: str
) -> tuple[RunOutcome, EventStore]:
    cfg = fast_config()
    registry = AdapterRegistry(cfg, builtin_ids=())
    for agent_id, kw in agents:
        registry.register(make_fake_entry(agent_id, base_dir=SCRIPTS, **kw))  # type: ignore[arg-type]
    (repo / ".aix").mkdir(exist_ok=True)
    store = await EventStore.open(repo / ".aix" / "aix.db")
    with anyio.fail_after(120):
        outcome = await execute_run(
            RunRequest(project_root=repo, goal=goal), registry=registry, store=store, config=cfg
        )
    return outcome, store


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


async def test_g2_multi_agent_jwt(repo: Path) -> None:
    agents: list[tuple[str, dict[str, object]]] = [
        ("fake-a", {"capabilities": {C.DESIGN: 0.95, C.RESEARCH: 0.9}}),
        (
            "fake-b",
            {
                "capabilities": {C.IMPLEMENT: 0.9, C.TEST: 0.9, C.DEBUG: 0.9, C.DOCUMENT: 0.8},
                "scripts": writer_scripts({"apply_patch": "patches/auth_v2_fixed.diff"}),
            },
        ),
        ("fake-reviewer", {"capabilities": {C.REVIEW: 0.95, C.SECURITY: 0.9}}),
    ]
    outcome, store = await go(repo, agents, "Add JWT authentication")
    try:
        assert outcome.status is RunStatus.COMPLETED, outcome
        assert len(outcome.tasks) >= 5
        assert {t.status for t in outcome.tasks} == {TaskStatus.COMPLETED}
        by_type = {t.type: t.agent_id for t in outcome.tasks}
        assert len({t.agent_id for t in outcome.tasks}) >= 2
        assert by_type["design"] == "fake-a" and by_type["implement"] == "fake-b"
        assert by_type["review"] == "fake-reviewer" != by_type["implement"]
        assert by_type["security_review"] == "fake-reviewer"  # auth goal is medium risk

        # the integrated branch is real and green
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp) / "wt"
            repos.git(repo, "worktree", "add", "-q", "--detach", str(wt), outcome.branch)
            try:
                assert repos.run_pytest(wt).returncode == 0
                assert (wt / "app" / "auth.py").exists() and (wt / "docs" / "notes.md").exists()
            finally:
                repos.git(repo, "worktree", "remove", "--force", str(wt))
    finally:
        await store.close()


async def test_g5_unavailable_primary_falls_back(repo: Path) -> None:
    caps = {C.IMPLEMENT: 0.9, C.TEST: 0.9, C.DEBUG: 0.9, C.DOCUMENT: 0.8, C.DESIGN: 0.9,
            C.RESEARCH: 0.9, C.REVIEW: 0.9}  # fmt: skip
    hello = {"apply_patch": "patches/hello.diff"}
    agents: list[tuple[str, dict[str, object]]] = [
        ("fake-primary", {"capabilities": caps, "health": "unavailable", "health_reason": "down"}),
        ("fake-fallback", {"capabilities": caps, "scripts": writer_scripts(hello)}),
        ("fake-spare", {"capabilities": {k: 0.5 for k in caps}, "scripts": writer_scripts(hello)}),
    ]
    outcome, store = await go(repo, agents, "Add a hello endpoint")
    try:
        assert outcome.status is RunStatus.COMPLETED
        got = {t.type: t.agent_id for t in outcome.tasks}
        assert "fake-primary" not in got.values()
        # everything on the fallback, except the review, which must be independent of the author
        assert got.pop("review") == "fake-spare"
        assert set(got.values()) == {"fake-fallback"}
        selected = await store.events(run_id=outcome.run_id, types=["agent.selected"])
        assert selected
        for e in selected:
            p = e.payload
            assert "ineligible:fake-primary:unavailable" in p.reason_codes  # type: ignore[attr-defined]
            assert "fake-primary" not in p.scores  # type: ignore[attr-defined]
            assert p.agent_id in ("fake-fallback", "fake-spare")  # type: ignore[attr-defined]
    finally:
        await store.close()
