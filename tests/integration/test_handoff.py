"""Agent B's prompt carries agent A's handoff, and every attempt's prompt is captured (M6, §15)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.core.orchestrator.executor import execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import Capability, RunStatus, TaskType
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.store.db import EventStore
from verif_env import fast_config

pytestmark = pytest.mark.anyio
C = Capability


def task(run_id: str, title: str, path: str, deps: list[str] | None = None) -> Task:
    return Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title=title, goal=f"do {title}",
        type=TaskType.IMPLEMENT, required_capabilities=[C.IMPLEMENT], file_scope=[path],
        depends_on=deps or [], verification=VerificationSpec(required=[K.BUILD]),
    )  # fmt: skip


async def test_dependent_prompt_contains_dependency_handoff(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "proj")
    (repo / ".aix").mkdir()
    (repo / "CLAUDE.md").write_text("Project rule: run make check.\n")
    repos.git(repo, "add", "-A")
    repos.git(repo, "commit", "-q", "-m", "rules")
    cfg = fast_config()
    registry = AdapterRegistry(cfg, builtin_ids=())
    scripts = [
        FakeScript(
            match=FakeMatch(prompt_contains="do first"),
            attempts=[FakeStep(write_files={"a.py": "x = 1\n"}, claim="A is finished, trust me.")],
        ),
        FakeScript(
            match=FakeMatch(prompt_contains="do second"),
            attempts=[FakeStep(write_files={"b.py": "y = 2\n"})],
        ),
    ]
    registry.register(make_fake_entry("fake-a", scripts=scripts, capabilities={C.IMPLEMENT: 0.9}))
    store = await EventStore.open(repo / ".aix" / "aix.db")
    try:
        wm = WorkspaceManager(repo)
        run_id = new_id(IdPrefix.RUN)
        await wm.create_run_branch(run_id)
        now = lambda: datetime.now(UTC)  # noqa: E731
        rec = RunRecorder(store, new_run(run_id, wm.root, "goal", cfg, now()), now)
        await rec.start()
        first = task(run_id, "first", "a.py")
        second = task(run_id, "second", "b.py", [first.id])
        graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=[first, second])
        await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
        with anyio.fail_after(120):
            outcome = await execute_graph(
                rec, wm, graph, registry=registry, config=cfg, backoff_scale=0
            )
        assert outcome.status is RunStatus.COMPLETED, outcome
        prompts = sorted((repo / ".aix" / "runs" / run_id).glob("*.prompt.txt"))
        assert len(prompts) == 2  # one captured prompt per attempt
        texts = [p.read_text() for p in prompts]
        dependent = next(t for t in texts if "do second" in t)
        independent = next(t for t in texts if "do first" in t)
        assert "DEPENDENCY HANDOFFS" not in independent
        assert first.id in dependent and "Files: a.py" in dependent
        assert "AGENT CLAIM (unverified):\n> A is finished, trust me." in dependent
        assert "Project rule: run make check." in dependent
    finally:
        await store.close()
