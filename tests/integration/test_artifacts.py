"""Standard artifacts and manifest of a finished run (M7.2, PLAYBOOK §21.3)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.artifacts.store import ObjectStore
from aix.core.orchestrator.executor import execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import ArtifactType, Capability, RunStatus, TaskType
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.store.db import EventStore
from verif_env import fast_config

pytestmark = pytest.mark.anyio
C = Capability


async def finished_run(tmp_path: Path, claim: str = "Done.") -> tuple[Path, str, EventStore]:
    repo = repos.materialize_sample_py(tmp_path / "proj")
    (repo / ".aix").mkdir()
    cfg = fast_config()
    registry = AdapterRegistry(cfg, builtin_ids=())
    step = FakeStep(write_files={"a.py": "x = 1\n"}, claim=claim)
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
    assert outcome.status is RunStatus.COMPLETED, outcome
    return repo, run_id, store


async def test_standard_artifacts_and_manifest(tmp_path: Path) -> None:
    repo, run_id, store = await finished_run(tmp_path)
    try:
        arts = await store.get_artifacts(run_id)
        by_type = {a.type for a in arts}
        assert {
            ArtifactType.PLAN, ArtifactType.PATCH, ArtifactType.VERIFICATION,
            ArtifactType.DECISION_LOG, ArtifactType.AGENT_TRACE, ArtifactType.MANIFEST,
            ArtifactType.PROMPT, ArtifactType.STREAM, ArtifactType.REPORT,
        } <= by_type  # fmt: skip
        assert arts[-1].type is ArtifactType.MANIFEST  # written last

        objects = ObjectStore(repo / ".aix" / "artifacts" / "objects")
        manifest = json.loads(await objects.get(arts[-1].sha256))
        listed = {e["artifact_id"]: e for e in manifest["artifacts"]}
        assert manifest["run_id"] == run_id and len(listed) == len(arts) - 1
        for art in arts[:-1]:
            entry = listed[art.id]
            assert entry["sha256"] == art.sha256 and await objects.verify(art.sha256)
        names = {e["name"] for e in manifest["artifacts"]}
        assert {"plan.json", "decision-log.json", "agent-trace.json"} <= names
        assert {"report.md", "report.html"} <= names
        md_sha = next(e["sha256"] for e in manifest["artifacts"] if e["name"] == "report.md")
        md = (await objects.get(md_sha)).decode()
        assert "COMPLETED" in md and "aix/run/" in md and "trust me" not in md
        assert any(n.startswith("patch/task_") and n.endswith(".diff") for n in names)
        assert set(arts[-1].provenance.inputs) == set(listed)
    finally:
        await store.close()


async def test_decision_log_and_trace_content(tmp_path: Path) -> None:
    repo, run_id, store = await finished_run(tmp_path, claim="All tests pass, trust me.")
    try:
        objects = ObjectStore(repo / ".aix" / "artifacts" / "objects")
        arts = {a.type: a for a in await store.get_artifacts(run_id)}
        log = json.loads(await objects.get(arts[ArtifactType.DECISION_LOG].sha256))
        trace = json.loads(await objects.get(arts[ArtifactType.AGENT_TRACE].sha256))
        assert log and log[0]["outcome"] == "accept"
        assert trace[0]["agent_id"] == "fake-a" and trace[0]["status"] == "completed"
        assert "trust me" not in json.dumps(trace)  # the claim is never trace content
        assert arts[ArtifactType.PATCH].provenance.producer.id == "fake-a"
    finally:
        await store.close()


async def test_same_run_content_is_deterministic(tmp_path: Path) -> None:
    repo, run_id, store = await finished_run(tmp_path)
    try:
        a = {x.type: x.sha256 for x in await store.get_artifacts(run_id)}
        assert len(a[ArtifactType.PLAN]) == 64
        objects = ObjectStore(repo / ".aix" / "artifacts" / "objects")
        plan = await objects.get(a[ArtifactType.PLAN])
        assert plan == (json.dumps(json.loads(plan), sort_keys=True, indent=2) + "\n").encode()
    finally:
        await store.close()
