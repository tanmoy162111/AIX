"""plan_run: intent -> planner selection -> post-processing -> events (M3.6, §13)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import JsonValue

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig, PlannerConfig
from aix.core.orchestrator.plan import PlanRunRequest, plan_run
from aix.domain.enums import Capability, CheckKind, RunStatus, TaskType
from aix.domain.errors import ConfigError
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio

PLAN: dict[str, JsonValue] = {
    "tasks": [
        {"key": "a", "title": "Look", "goal": "look around", "type": "inspect"},
        {
            "key": "b",
            "title": "Build",
            "goal": "build it",
            "type": "implement",
            "depends_on": ["a"],
            "file_scope": ["src/**"],
        },
        {
            "key": "c",
            "title": "Docs",
            "goal": "doc it",
            "type": "document",
            "depends_on": ["a"],
            "file_scope": ["src/docs/**"],
        },
    ]
}
DESIGNER = {Capability.DESIGN: 0.9, Capability.IMPLEMENT: 0.5}


def scripted(output: JsonValue) -> list[FakeScript]:
    return [FakeScript(match=FakeMatch(), attempts=[FakeStep(planner_output=output)])]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


async def env(
    root: Path,
    *agents: tuple[str, dict[str, object]],
    provider: str = "auto",
    max_tasks: int = 12,
):  # type: ignore[no-untyped-def]
    cfg = AixConfig(planner=PlannerConfig(provider=provider, max_tasks=max_tasks))
    registry = AdapterRegistry(cfg, builtin_ids=())
    for agent_id, kw in agents:
        registry.register(make_fake_entry(agent_id, **kw))  # type: ignore[arg-type]
    (root / ".aix").mkdir(exist_ok=True)
    store = await EventStore.open(root / ".aix" / "aix.db")
    return registry, store, cfg


async def plan(root: Path, *agents, goal: str = "Add a retry option", **kw):  # type: ignore[no-untyped-def]
    skill = kw.pop("skill", None)
    registry, store, cfg = await env(root, *agents, **kw)
    try:
        res = await plan_run(
            PlanRunRequest(project_root=root, goal=goal, skill=skill),
            registry=registry,
            store=store,
            config=cfg,
        )
        events = await store.events(run_id=res.run_id)
        run = await store.get_run(res.run_id)
        tasks = await store.get_tasks(res.run_id)
        return res, events, run, tasks, store
    finally:
        await store.close()


async def test_template_provider_needs_no_git_and_records_the_plan(tmp_path: Path) -> None:
    res, events, run, tasks, _ = await plan(tmp_path, provider="template")
    assert res.planner == "template" and not (tmp_path / ".git").exists()
    assert run is not None and run.status is RunStatus.PLANNED
    assert run.intent is not None and run.intent.kind == "coding"
    assert [t.id for t in tasks] == [t.id for t in res.graph.tasks]
    types = [e.type for e in events]
    assert types[:3] == ["run.created", "run.state_changed", "run.planned"]
    assert types.count("task.created") == len(res.graph.tasks)
    assert types[-1] == "run.state_changed"


async def test_write_tasks_get_verification_defaults(tmp_path: Path) -> None:
    res, *_ = await plan(tmp_path, provider="template")
    impl = next(t for t in res.graph.tasks if t.type is TaskType.IMPLEMENT)
    assert {CheckKind.BUILD, CheckKind.TESTS, CheckKind.LINT} <= set(impl.verification.required)
    review = next(t for t in res.graph.tasks if t.type is TaskType.REVIEW)
    assert not review.verification.required


async def test_auto_uses_a_ready_design_capable_agent(repo: Path) -> None:
    res, _, run, _, _ = await plan(
        repo, ("fake-planner", {"capabilities": DESIGNER, "scripts": scripted(PLAN)})
    )
    assert res.planner == "agent:fake-planner"
    assert [t.title for t in res.graph.tasks] == ["Look", "Build", "Docs"]
    assert run is not None and run.status is RunStatus.PLANNED
    assert (repos.git(repo, "branch", "--list", f"aix/run/{res.run_id}").stdout).strip()
    assert list((repo / ".aix" / "worktrees").iterdir()) == []
    # overlapping unordered writers were ordered by post-processing
    docs = next(t for t in res.graph.tasks if t.title == "Docs")
    build = next(t for t in res.graph.tasks if t.title == "Build")
    assert build.id in docs.depends_on


async def test_builtin_fake_id_is_never_auto_selected(repo: Path) -> None:
    res, *_ = await plan(repo, ("fake", {"scripts": scripted(PLAN)}))
    assert res.planner == "template"


async def test_unavailable_or_weak_agents_are_skipped(repo: Path) -> None:
    res, *_ = await plan(
        repo,
        ("fake-down", {"capabilities": DESIGNER, "health": "unavailable"}),
        ("fake-weak", {"capabilities": {Capability.DESIGN: 0.3}}),
    )
    assert res.planner == "template"


async def test_best_design_prior_wins_ties_by_id(repo: Path) -> None:
    res, *_ = await plan(
        repo,
        ("fake-b", {"capabilities": {Capability.DESIGN: 0.8}, "scripts": scripted(PLAN)}),
        ("fake-a", {"capabilities": {Capability.DESIGN: 0.8}, "scripts": scripted(PLAN)}),
    )
    assert res.planner == "agent:fake-a"


async def test_garbage_from_agent_falls_back_with_a_warning(repo: Path) -> None:
    res, *_ = await plan(
        repo, ("fake-planner", {"capabilities": DESIGNER, "scripts": scripted("not json")})
    )
    assert res.planner == "template"
    assert any("fake-planner" in w and "fallback" in w for w in res.warnings)
    assert any(t.type is TaskType.IMPLEMENT for t in res.graph.tasks)


async def test_explicit_agent_provider(repo: Path) -> None:
    res, *_ = await plan(
        repo,
        ("fake-x", {"capabilities": {Capability.DESIGN: 0.1}, "scripts": scripted(PLAN)}),
        provider="agent:fake-x",
    )
    assert res.planner == "agent:fake-x"


async def test_explicit_unknown_agent_is_a_config_error(repo: Path) -> None:
    with pytest.raises(ConfigError, match="unknown agent"):
        await plan(repo, provider="agent:nope")


async def test_explicit_unavailable_agent_falls_back_to_template(repo: Path) -> None:
    res, *_ = await plan(repo, ("fake-x", {"health": "unavailable"}), provider="agent:fake-x")
    assert res.planner == "template" and any("fake-x" in w for w in res.warnings)


async def test_max_tasks_is_enforced(tmp_path: Path) -> None:
    res, *_ = await plan(tmp_path, provider="template", max_tasks=3)
    assert len(res.graph.tasks) == 3
    assert any("merged" in w for w in res.warnings)


async def test_skill_override_and_high_risk_intent(tmp_path: Path) -> None:
    res, *_ = await plan(
        tmp_path, goal="Deploy the fix to production", provider="template", skill="bugfix"
    )
    assert res.intent.risk == "high"
    assert {t.skill for t in res.graph.tasks} == {"bugfix"}


async def test_replay_reproduces_the_projection(tmp_path: Path) -> None:
    res, _, _, tasks, _ = await plan(tmp_path, provider="template")
    store = await EventStore.open(tmp_path / ".aix" / "aix.db")
    try:
        await store.rebuild_projections()
        assert [t.model_dump() for t in await store.get_tasks(res.run_id)] == [
            t.model_dump() for t in tasks
        ]
    finally:
        await store.close()
