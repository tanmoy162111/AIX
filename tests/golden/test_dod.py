"""PLAYBOOK §31 Definition of Done walk-through, with fake agents (M10.3).

One JWT run over the fixture repo with the real toolchain: plan, capability routing to several
agents, worktrees, handoffs, executed checks, independent review, recorded decisions, a retry
after failed verification, integration on the run branch, a verifiable bundle, and decision
replay (item 5). Item 4 (approval through a human channel) is covered by G4.
"""

from __future__ import annotations

import json
import os
import shutil
import zipfile
from pathlib import Path

import anyio
import anyio.to_thread
import pytest
from typer.testing import CliRunner, Result

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.cli.main import app
from aix.config.schema import AixConfig, PlannerConfig
from aix.core.orchestrator.executor import RunRequest, execute_run
from aix.domain.enums import Capability, RunStatus, TaskStatus
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio
C = Capability
GOAL = "Add JWT authentication to this repository"
SCRIPTS = repos.FIXTURES / "agent_scripts"
runner = CliRunner()


async def cli(*args: str) -> Result:
    """Run the CLI off the event loop (its commands call ``anyio.run`` themselves)."""
    return await anyio.to_thread.run_sync(lambda: runner.invoke(app, list(args)))


def step(**kw: object) -> FakeStep:
    return FakeStep.model_validate(kw)


def scripts() -> list[FakeScript]:
    return [
        FakeScript(
            match=FakeMatch(task_type="implement"),
            attempts=[
                step(apply_patch="patches/auth_v1_broken.diff", claim="Done. All tests pass."),
                step(apply_patch="patches/auth_v2_fixed.diff", claim="Fixed the failing tests."),
            ],
        ),
        FakeScript(
            match=FakeMatch(task_type="test"),
            attempts=[step(write_files={"tests/test_extra.py": "def test_extra():\n    pass\n"})],
        ),
        FakeScript(
            match=FakeMatch(task_type="document"),
            attempts=[step(write_files={"docs/notes.md": "# Notes\n"})],
        ),
    ]


async def test_definition_of_done_walkthrough(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "proj")
    cfg = AixConfig(planner=PlannerConfig(provider="template"))
    registry = AdapterRegistry(cfg, builtin_ids=())
    for agent_id, kw in [
        ("fake-a", {"capabilities": {C.DESIGN: 0.95, C.RESEARCH: 0.9}}),
        (
            "fake-b",
            {
                "capabilities": {C.IMPLEMENT: 0.9, C.TEST: 0.9, C.DEBUG: 0.9, C.DOCUMENT: 0.8},
                "scripts": scripts(),
            },
        ),
        ("fake-reviewer", {"capabilities": {C.REVIEW: 0.95, C.SECURITY: 0.9}}),
    ]:
        registry.register(make_fake_entry(agent_id, base_dir=SCRIPTS, **kw))  # type: ignore[arg-type]
    (repo / ".aix").mkdir(exist_ok=True)
    store = await EventStore.open(repo / ".aix" / "aix.db")
    try:
        with anyio.fail_after(300):
            outcome = await execute_run(
                RunRequest(project_root=repo, goal=GOAL), registry=registry, store=store, config=cfg
            )
        assert outcome.status is RunStatus.COMPLETED, outcome
        # plans >= 5 tasks and routes to >= 2 agents
        assert len(outcome.tasks) >= 5
        assert {t.status for t in outcome.tasks} == {TaskStatus.COMPLETED}
        assert len({t.agent_id for t in outcome.tasks}) >= 2
        implement = next(t for t in outcome.tasks if t.type == "implement")
        review = next(t for t in outcome.tasks if t.type == "review")
        assert implement.attempts == 2 and review.agent_id != implement.agent_id  # retried
        # every decision is recorded, incl. the retry after real (executed) test failures
        decisions = await store.get_decisions(outcome.run_id)
        assert "retry" in {d.outcome.value for d in decisions}
        assert decisions[-1].outcome.value == "accept"
        # integrated on the run branch, which is green
        assert repos.git(repo, "show", f"{outcome.branch}:app/auth.py").returncode == 0
    finally:
        await store.close()

    # a verifiable bundle
    exported = await cli(
        "artifact", "export", outcome.run_id, "--bundle", "--json", "--project", str(repo)
    )
    assert exported.exit_code == 0, exported.output
    bundle = Path(json.loads(exported.output)["bundle"])
    verified = await cli("artifact", "verify", str(bundle))
    assert verified.exit_code == 0, verified.output
    if keep := os.environ.get("AIX_DOD_BUNDLE_OUT"):  # attach the bundle to the final report
        shutil.copy(bundle, keep)

    # item 5: replay reproduces the recorded decisions exactly
    log = tmp_path / "decision-log.json"
    with zipfile.ZipFile(bundle) as z:
        name = next(n for n in z.namelist() if n.endswith("decision-log.json"))
        log.write_bytes(z.read(name))
    replayed = await cli("dev", "eval-decisions", "--replay", str(log))
    assert replayed.exit_code == 0, replayed.output
    assert "0 differ" in replayed.output
