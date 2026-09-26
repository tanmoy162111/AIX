"""Shared fixture: a finished single-task fake run with its artifacts written."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
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

C = Capability


async def finished_run(
    tmp_path: Path,
    claim: str = "Done.",
    *,
    cfg: AixConfig | None = None,
    usage: dict[str, object] | None = None,
    expect: RunStatus = RunStatus.COMPLETED,
) -> tuple[Path, str, EventStore]:
    repo = repos.materialize_sample_py(tmp_path / "proj")
    (repo / ".aix").mkdir()
    cfg = cfg or fast_config()
    registry = AdapterRegistry(cfg, builtin_ids=())
    step = FakeStep.model_validate(
        {"write_files": {"a.py": "x = 1\n"}, "claim": claim, "usage": usage}
    )
    registry.register(
        make_fake_entry(
            "fake-a", scripts=[FakeScript(match=FakeMatch(task_type="implement"), attempts=[step])],
            capabilities={C.IMPLEMENT: 0.9},
        )
    )  # fmt: skip
    store = await EventStore.open(repo / ".aix" / "aix.db")
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    now = lambda: datetime.now(UTC)  # noqa: E731
    rec = RunRecorder(store, new_run(run_id, wm.root, "goal", cfg, now()), now)
    await rec.start()
    task = Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title="first", goal="do first",
        type=TaskType.IMPLEMENT, required_capabilities=[C.IMPLEMENT], file_scope=["a.py"],
        verification=VerificationSpec(required=[K.BUILD, K.TESTS]),
    )  # fmt: skip
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=[task])
    await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
    with anyio.fail_after(120):
        outcome = await execute_graph(
            rec, wm, graph, registry=registry, config=cfg, backoff_scale=0
        )
    assert outcome.status is expect, outcome
    return repo, run_id, store
