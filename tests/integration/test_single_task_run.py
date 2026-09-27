"""Fake agent end to end: run -> worktree -> execute -> diff -> events -> run branch (M2.11)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import repos
from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import load_scripts
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
from aix.core.orchestrator.single import SingleTaskRequest, run_single_task
from aix.domain.enums import (
    AttemptStatus,
    FailureClass,
    RunStatus,
    TaskStatus,
)
from aix.domain.errors import NoEligibleAgent, ToolFailure
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio

SCRIPTS = repos.FIXTURES / "agent_scripts"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


async def make_env(
    repo: Path, script: str | None, *, config: AixConfig | None = None, **fake: object
):  # type: ignore[no-untyped-def]
    cfg = config or AixConfig.model_validate({"agents": {"enabled": ["fake"]}})
    registry = AdapterRegistry(cfg, builtin_ids=())
    scripts = load_scripts(SCRIPTS / script) if script else []
    registry.register(make_fake_entry("fake", scripts=scripts, base_dir=SCRIPTS, **fake))  # type: ignore[arg-type]
    store = await EventStore.open(repo / ".aix" / "aix.db")
    return registry, store, cfg


async def go(
    repo: Path,
    script: str | None,
    *,
    config: AixConfig | None = None,
    file_scope: list[str] | None = None,
    keep_worktrees: bool = False,
    fake: dict[str, Any] | None = None,
):  # type: ignore[no-untyped-def]
    registry, store, cfg = await make_env(repo, script, config=config, **(fake or {}))
    try:
        req = SingleTaskRequest(
            project_root=repo,
            goal="Add a hello endpoint",
            agent_id="fake",
            file_scope=file_scope or ["**"],
            keep_worktrees=keep_worktrees,
        )
        result = await run_single_task(req, registry=registry, store=store, config=cfg)
        events = await store.events(run_id=result.run_id)
        run = await store.get_run(result.run_id)
        tasks = await store.get_tasks(result.run_id)
        attempts = await store.get_attempts(tasks[0].id)
        return result, events, run, tasks, attempts, store
    except BaseException:
        await store.close()
        raise


def git_head(repo: Path, ref: str) -> str:
    return repos.git(repo, "rev-parse", ref).stdout.strip()


async def test_successful_run_lands_on_the_run_branch_only(repo: Path) -> None:
    main_before = git_head(repo, "main")
    result, _, run, tasks, attempts, store = await go(repo, "hello.yaml")
    try:
        assert result.status is RunStatus.COMPLETED and result.exit_code == 0
        assert result.branch == f"aix/run/{result.run_id}"
        # the change is on the run branch, as a merge commit; the user's branch is untouched
        assert "/hello" in repos.git(repo, "show", f"{result.branch}:app/handler.py").stdout
        parents = repos.git(repo, "rev-list", "--parents", "-n", "1", result.branch).stdout.split()
        assert len(parents) == 3  # sha + two parents
        assert git_head(repo, "main") == main_before
        assert repos.git(repo, "status", "--porcelain", "--untracked-files=no").stdout == ""
        # projections
        assert run is not None and run.status is RunStatus.COMPLETED and run.finished_at
        assert [t.status for t in tasks] == [TaskStatus.COMPLETED]
        assert [a.status for a in attempts] == [AttemptStatus.COMPLETED]
        res = await store.get_result(attempts[0].id)
        assert res is not None
        assert res.claim == "Done. All tests pass."  # stored as a claim, not evidence
        assert res.diff.paths == ["app/handler.py", "tests/test_hello.py"]
        assert res.usage.cost_usd == 0.02 and res.diff.patch_sha256
    finally:
        await store.close()


async def test_event_sequence_is_complete_and_ordered(repo: Path) -> None:
    _, events, _, _, _, store = await go(repo, "hello.yaml")
    try:
        types = [e.type for e in events]
        expected_in_order = [
            "run.created", "run.state_changed", "run.planned", "task.created", "run.state_changed",
            "run.state_changed", "task.state_changed", "agent.selected", "task.state_changed",
            "workspace.created", "attempt.created", "task.state_changed", "attempt.started",
            "attempt.finished", "task.state_changed", "task.state_changed", "task.state_changed",
            "task.state_changed", "workspace.merged", "task.state_changed", "workspace.removed",
            "run.state_changed", "run.state_changed", "run.completed",
        ]  # fmt: skip
        it = iter(types)
        assert all(step in it for step in expected_in_order), types
        assert types.count("agent.tool_called") == 1
        assert types.count("agent.output") == 1
        assert [e.seq for e in events] == sorted(e.seq for e in events)
    finally:
        await store.close()


async def test_lying_agent_with_no_changes_fails_the_task(repo: Path) -> None:
    base = git_head(repo, "main")
    result, events, _, tasks, _, store = await go(repo, "noop_lying.yaml")
    try:
        assert result.status is RunStatus.FAILED and result.exit_code == 1
        assert result.failure is FailureClass.AGENT_NO_CHANGES
        assert tasks[0].status is TaskStatus.FAILED
        assert git_head(repo, result.branch) == base  # nothing was merged
        assert "run.failed" in [e.type for e in events]
    finally:
        await store.close()


async def test_agent_crash_is_recorded_with_its_failure_class(repo: Path) -> None:
    result, _, _, _, attempts, store = await go(repo, "crash.yaml")
    try:
        assert result.failure is FailureClass.AGENT_FAILURE and result.exit_code == 1
        assert attempts[0].status is AttemptStatus.FAILED
        res = await store.get_result(attempts[0].id)
        assert res is not None and res.exit_code == 1
    finally:
        await store.close()


async def test_scope_violation_is_recorded_and_not_merged(repo: Path) -> None:
    base = git_head(repo, "main")
    result, events, _, tasks, _, store = await go(
        repo, "outside_scope.yaml", file_scope=["app/**", "tests/**"]
    )
    try:
        assert result.failure is FailureClass.SCOPE_VIOLATION
        violations = [e for e in events if e.type == "policy.violation"]
        assert len(violations) == 1 and violations[0].payload.path == "deploy/production.yaml"  # type: ignore[attr-defined]
        assert tasks[0].status is TaskStatus.FAILED
        assert git_head(repo, result.branch) == base
    finally:
        await store.close()


async def test_default_scope_allows_the_same_change(repo: Path) -> None:
    result, _, _, _, _, store = await go(repo, "outside_scope.yaml")
    try:
        assert result.status is RunStatus.COMPLETED
    finally:
        await store.close()


async def test_keep_worktrees_and_default_cleanup(repo: Path) -> None:
    result, _, _, _, attempts, store = await go(repo, "hello.yaml", keep_worktrees=True)
    try:
        assert (repo / ".aix" / "worktrees" / attempts[0].id).is_dir()
    finally:
        await store.close()
    repo2_result, _, _, _, attempts2, store2 = await go(repo, "hello.yaml")
    try:
        assert not (repo / ".aix" / "worktrees" / attempts2[0].id).exists()
        assert (
            repos.git(repo, "rev-parse", "--verify", f"aix/att/{attempts2[0].id}").returncode == 0
        )
        assert repo2_result.branch != result.branch
    finally:
        await store2.close()


async def test_stream_file_exists_and_is_referenced(repo: Path) -> None:
    result, _, _, _, attempts, store = await go(repo, "hello.yaml")
    try:
        res = await store.get_result(attempts[0].id)
        assert res is not None
        assert (
            res.stream_path
            == repo / ".aix" / "runs" / result.run_id / f"{attempts[0].id}.stream.jsonl"
        )
        assert res.stream_path.read_text().count("\n") >= 3
    finally:
        await store.close()


async def test_timeout_is_a_failed_attempt(repo: Path) -> None:
    cfg = AixConfig.model_validate(
        {"execution": {"attempt_timeout_s": 1}, "agents": {"enabled": ["fake"]}}
    )
    result, _, _, _, attempts, store = await go(repo, "slow.yaml", config=cfg)
    try:
        assert result.failure is FailureClass.TIMEOUT
        assert attempts[0].status is AttemptStatus.FAILED
    finally:
        await store.close()


async def test_unavailable_agent_raises_before_creating_anything(repo: Path) -> None:
    registry, store, cfg = await make_env(repo, None, health="unavailable", health_reason="down")
    try:
        req = SingleTaskRequest(project_root=repo, goal="x", agent_id="fake")
        with pytest.raises(NoEligibleAgent, match="down"):
            await run_single_task(req, registry=registry, store=store, config=cfg)
        assert await store.count_events() == 0
    finally:
        await store.close()


async def test_dirty_tree_is_refused_unless_allowed(repo: Path) -> None:
    (repo / "README.md").write_text("dirty\n")
    registry, store, cfg = await make_env(repo, "hello.yaml")
    try:
        req = SingleTaskRequest(project_root=repo, goal="x", agent_id="fake")
        with pytest.raises(ToolFailure, match="uncommitted"):
            await run_single_task(req, registry=registry, store=store, config=cfg)
        assert await store.count_events() == 0
        ok = SingleTaskRequest(project_root=repo, goal="x", agent_id="fake", allow_dirty=True)
        result = await run_single_task(ok, registry=registry, store=store, config=cfg)
        assert result.status is RunStatus.COMPLETED
        assert (repo / "README.md").read_text() == "dirty\n"
    finally:
        await store.close()


async def test_replay_of_the_recorded_events_matches_the_live_projections(repo: Path) -> None:
    _, _, _, _, _, store = await go(repo, "hello.yaml")
    try:
        before = {t: await store.dump_table(t) for t in ("runs", "tasks", "attempts", "checks")}
        await store.rebuild_projections()
        assert before == {t: await store.dump_table(t) for t in before}
    finally:
        await store.close()


def test_fake_adapter_type_is_used() -> None:
    assert FakeAdapter("fake").id == "fake"
