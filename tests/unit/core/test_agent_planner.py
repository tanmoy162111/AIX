from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from pydantic import JsonValue

import repos
from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.core.planner.agent import (
    AgentPlanner,
    PlanInvalid,
    extract_json_object,
    make_adapter_runner,
    parse_plan,
    render_planner_prompt,
)
from aix.core.planner.template import TemplatePlanner
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import Capability, CheckKind, TaskType
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, RepoFacts
from aix.skills.registry import SkillRegistry

pytestmark = pytest.mark.anyio

REG = SkillRegistry.builtin()
INTENT = Intent(goal="Add a retry option", kind="coding", risk="medium")
FACTS = RepoFacts(languages=["python"], package_managers=["uv"], test_commands=["pytest -q"])
SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "planner.txt"

GOOD: dict[str, JsonValue] = {
    "tasks": [
        {"key": "a", "title": "Inspect", "goal": "look", "type": "inspect"},
        {
            "key": "b",
            "title": "Implement",
            "goal": "do it",
            "type": "implement",
            "depends_on": ["a"],
            "file_scope": ["src/**"],
            "required_capabilities": ["implement", "telepathy"],
            "verification": {"required": ["tests"], "optional": ["lint"]},
        },
        {"key": "c", "title": "Review", "goal": "check", "type": "review", "depends_on": ["b"]},
    ]
}


class Scripted:
    """A runner returning queued outputs and recording the prompts it was given."""

    def __init__(self, *outputs: str | Exception) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    async def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        out = self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]
        if isinstance(out, Exception):
            raise out
        return out


def planner(runner: Callable[[str], Awaitable[str]], max_tasks: int = 12) -> AgentPlanner:
    return AgentPlanner(REG, runner, TemplatePlanner(REG), max_tasks=max_tasks)


# ---- extraction ----------------------------------------------------------------------------


def test_extract_last_object_ignores_nested_and_prose() -> None:
    text = 'thinking {"draft": 1}\n```json\n{"tasks": [{"x": {"y": 2}}]}\n```\nbye'
    assert extract_json_object(text) == {"tasks": [{"x": {"y": 2}}]}


def test_extract_prefers_last_object_with_tasks_key() -> None:
    assert extract_json_object('{"tasks": []} then {"note": 1}') == {"tasks": []}


@pytest.mark.parametrize("text", ["", "no json here", "{broken", "[1, 2]"])
def test_extract_returns_none_without_an_object(text: str) -> None:
    assert extract_json_object(text) is None


# ---- parse / validate ----------------------------------------------------------------------


def test_parse_maps_keys_to_ids_and_normalizes() -> None:
    graph, warnings = parse_plan(json.dumps(GOOD), INTENT, new_id(IdPrefix.RUN), REG)
    a, b, c = graph.tasks
    assert b.depends_on == [a.id] and c.depends_on == [b.id]
    assert b.type is TaskType.IMPLEMENT and b.file_scope == ["src/**"]
    assert b.required_capabilities == [Capability.IMPLEMENT]  # unknown capability dropped
    assert any("telepathy" in w for w in warnings)
    assert b.verification.required == [CheckKind.TESTS]
    assert b.risk == "medium"  # inherited from the intent


@pytest.mark.parametrize(
    ("mutate", "msg"),
    [
        (lambda d: d.update(tasks=[]), "at least one task"),
        (lambda d: d["tasks"][0].update(type="teleport"), "type"),
        (lambda d: d["tasks"][1].update(depends_on=["zzz"]), "unknown dependency 'zzz'"),
        (lambda d: d["tasks"][0].update(depends_on=["c"]), "cycle"),
        (lambda d: d["tasks"][1].update(key="a"), "duplicate"),
        (lambda d: d["tasks"][1].update(skill="nope"), "unknown skill 'nope'"),
        (lambda d: d["tasks"][0].update(bogus=1), "bogus"),
        (lambda d: d["tasks"][0].update(depends_on=["a"]), "itself"),
    ],
)
def test_parse_rejects_invalid_plans(mutate: Callable[[dict], None], msg: str) -> None:  # type: ignore[type-arg]
    doc = json.loads(json.dumps(GOOD))
    mutate(doc)
    with pytest.raises(PlanInvalid, match=msg):
        parse_plan(json.dumps(doc), INTENT, new_id(IdPrefix.RUN), REG)


def test_parse_rejects_text_without_json() -> None:
    with pytest.raises(PlanInvalid, match="no JSON object"):
        parse_plan("I refuse", INTENT, new_id(IdPrefix.RUN), REG)


# ---- prompt --------------------------------------------------------------------------------


def test_prompt_snapshot() -> None:
    text = render_planner_prompt(INTENT, FACTS, REG, max_tasks=12)
    if not SNAPSHOT.exists():  # pragma: no cover - first run only
        SNAPSHOT.write_text(text)
    assert text == SNAPSHOT.read_text()


def test_prompt_contains_required_sections_and_no_vendor() -> None:
    text = render_planner_prompt(INTENT, FACTS, REG, max_tasks=7)
    for section in ("ROLE", "GOAL", "INTENT", "REPO FACTS", "AVAILABLE TASK TYPES", "SKILLS"):
        assert section in text
    assert "Add a retry option" in text and "7" in text and "bugfix" in text
    assert not any(v in text.lower() for v in ("claude", "codex", "gemini", "opencode"))


# ---- planner: repair loop and fallback -----------------------------------------------------


async def run_plan(p: AgentPlanner) -> tuple[list[TaskType], str]:
    outcome = await p.plan_with_report(INTENT, FACTS, new_id(IdPrefix.RUN))
    return [t.type for t in outcome.graph.tasks], outcome.source


async def test_valid_first_answer_is_used() -> None:
    r = Scripted(json.dumps(GOOD))
    types, source = await run_plan(planner(r))
    assert source == "agent" and types == [TaskType.INSPECT, TaskType.IMPLEMENT, TaskType.REVIEW]
    assert len(r.prompts) == 1


async def test_repair_prompt_includes_errors_verbatim_then_succeeds() -> None:
    bad = json.loads(json.dumps(GOOD))
    bad["tasks"][1]["depends_on"] = ["zzz"]
    r = Scripted(json.dumps(bad), json.dumps(GOOD))
    _, source = await run_plan(planner(r))
    assert source == "agent" and len(r.prompts) == 2
    assert "unknown dependency 'zzz'" in r.prompts[1]
    assert r.prompts[1].startswith(r.prompts[0])


async def test_two_repairs_then_fall_back_to_template() -> None:
    r = Scripted("not json")
    p = planner(r)
    outcome = await p.plan_with_report(INTENT, FACTS, new_id(IdPrefix.RUN))
    assert outcome.source == "template_fallback"
    assert len(r.prompts) == 3  # first try + 2 repairs
    assert TaskType.IMPLEMENT in {t.type for t in outcome.graph.tasks}
    assert outcome.errors and "no JSON object" in outcome.errors[-1]


async def test_agent_failure_falls_back_immediately() -> None:
    r = Scripted(RuntimeError("agent exploded"))
    outcome = await planner(r).plan_with_report(INTENT, FACTS, new_id(IdPrefix.RUN))
    assert outcome.source == "template_fallback" and len(r.prompts) == 1
    assert "agent exploded" in outcome.errors[0]


async def test_plan_protocol_method_returns_graph() -> None:
    graph = await planner(Scripted(json.dumps(GOOD))).plan(INTENT, FACTS, new_id(IdPrefix.RUN))
    assert len(graph.tasks) == 3


async def test_skill_override_is_used_by_fallback() -> None:
    graph = await planner(Scripted("nope")).plan(INTENT, FACTS, new_id(IdPrefix.RUN), "bugfix")
    assert graph.tasks[0].skill == "bugfix"


# ---- adapter-backed runner (fake agent, read-only throwaway worktree) ----------------------


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


def fake_planner_agent(step: FakeStep) -> FakeAdapter:
    return FakeAdapter("fake", scripts=[FakeScript(match=FakeMatch(), attempts=[step])])


async def test_adapter_runner_returns_claim_and_cleans_up(repo: Path) -> None:
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    adapter = fake_planner_agent(FakeStep(planner_output=GOOD))
    runner = make_adapter_runner(adapter, wm, run_id, timeout_s=30)
    graph = await AgentPlanner(REG, runner, TemplatePlanner(REG)).plan(INTENT, FACTS, run_id)
    assert len(graph.tasks) == 3
    assert list(wm.worktrees_dir.iterdir()) == []


async def test_adapter_runner_rejects_a_planner_that_wrote_files(repo: Path) -> None:
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    adapter = fake_planner_agent(FakeStep(planner_output=GOOD, write_files={"oops.txt": "x"}))
    runner = make_adapter_runner(adapter, wm, run_id, timeout_s=30)
    outcome = await AgentPlanner(REG, runner, TemplatePlanner(REG)).plan_with_report(
        INTENT, FACTS, run_id
    )
    assert outcome.source == "template_fallback"
    assert "modified" in outcome.errors[0]
    assert list(wm.worktrees_dir.iterdir()) == []


async def test_adapter_runner_failed_agent_falls_back(repo: Path) -> None:
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    adapter = fake_planner_agent(FakeStep(exit_code=1, stderr="boom", claim=None))
    runner = make_adapter_runner(adapter, wm, run_id, timeout_s=30)
    outcome = await AgentPlanner(REG, runner, TemplatePlanner(REG)).plan_with_report(
        INTENT, FACTS, run_id
    )
    assert outcome.source == "template_fallback"
