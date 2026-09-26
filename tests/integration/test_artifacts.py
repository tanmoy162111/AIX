"""Standard artifacts and manifest of a finished run (M7.2, PLAYBOOK §21.3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aix.artifacts.store import ObjectStore
from aix.domain.enums import ArtifactType
from run_env import finished_run

pytestmark = pytest.mark.anyio


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
