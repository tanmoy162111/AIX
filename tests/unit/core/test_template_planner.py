from __future__ import annotations

import anyio
import pytest

from aix.core.planner.template import TemplatePlanner, select_skill
from aix.domain.enums import Capability, TaskType
from aix.domain.errors import ConfigError
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, RepoFacts, TaskGraph
from aix.skills.registry import SkillRegistry

REG = SkillRegistry.builtin()
FACTS = RepoFacts(languages=["python"])


def plan(goal: str, kind: str = "coding", risk: str = "low", skill: str | None = None) -> TaskGraph:
    intent = Intent(goal=goal, kind=kind, risk=risk)  # type: ignore[arg-type]
    return anyio.run(TemplatePlanner(REG).plan, intent, FACTS, new_id(IdPrefix.RUN), skill)


def by_type(g: TaskGraph) -> dict[TaskType, list[str]]:
    out: dict[TaskType, list[str]] = {}
    for t in g.tasks:
        out.setdefault(t.type, []).append(t.id)
    return out


def test_default_coding_template_shape_low_risk() -> None:
    g = plan("Add a retry option")
    assert [t.type for t in g.tasks] == [
        TaskType.INSPECT,
        TaskType.DESIGN,
        TaskType.IMPLEMENT,
        TaskType.TEST,
        TaskType.REVIEW,
        TaskType.DOCUMENT,
    ]
    ids = by_type(g)
    impl = ids[TaskType.IMPLEMENT][0]
    test, review = ids[TaskType.TEST][0], ids[TaskType.REVIEW][0]
    tasks = {t.id: t for t in g.tasks}
    assert tasks[test].depends_on == [impl] == tasks[review].depends_on  # parallel after implement
    assert set(tasks[ids[TaskType.DOCUMENT][0]].depends_on) == {test, review}


def test_security_review_added_when_risk_medium_or_higher() -> None:
    for risk in ("medium", "high"):
        g = plan("Change auth", risk=risk)
        assert TaskType.SECURITY_REVIEW in by_type(g)
    assert TaskType.SECURITY_REVIEW not in by_type(plan("Add a flag", risk="low"))


def test_goal_substituted_and_metadata_set() -> None:
    g = plan("Add {weird} braces %s", risk="medium")
    impl = next(t for t in g.tasks if t.type is TaskType.IMPLEMENT)
    assert impl.goal == "Add {weird} braces %s"
    assert impl.skill == "feature-implementation"
    assert impl.risk == "medium"
    assert impl.file_scope == ["**"]
    assert Capability.IMPLEMENT in impl.required_capabilities
    assert impl.run_id == g.run_id
    inspect = g.tasks[0]
    assert inspect.file_scope == [] and inspect.depends_on == []


def test_skill_verification_attached_to_write_tasks_only() -> None:
    g = plan("Add a flag")
    for t in g.tasks:
        if t.file_scope:
            assert t.verification.required, t.type
        else:
            assert not t.verification.required, t.type


def test_excluded_steps_are_bridged() -> None:
    # security_review excluded at low risk: nothing may depend on a missing step.
    g = plan("Add a flag", risk="low")
    ids = {t.id for t in g.tasks}
    assert all(set(t.depends_on) <= ids for t in g.tasks)


def test_skill_that_requires_security_review_includes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    skill = REG.get("feature-implementation")
    forced = skill.model_copy(
        update={
            "verification": skill.verification.model_copy(update={"requires_security_review": True})
        }
    )
    reg = SkillRegistry([forced])
    intent = Intent(goal="x", kind="coding", risk="low")
    g = anyio.run(TemplatePlanner(reg).plan, intent, FACTS, new_id(IdPrefix.RUN), None)
    assert TaskType.SECURITY_REVIEW in by_type(g)


def test_review_only_skill_has_single_read_only_task() -> None:
    g = plan("Review src/x.py", kind="review")
    assert [t.type for t in g.tasks] == [TaskType.REVIEW]
    assert g.tasks[0].file_scope == []


@pytest.mark.parametrize(
    ("goal", "kind", "expected"),
    [
        ("Add a retry option", "coding", "feature-implementation"),
        ("Fix the crash on empty config", "coding", "bugfix"),
        ("Add unit tests for the parser", "coding", "write-tests"),
        ("Review the diff", "review", "code-review"),
        ("Audit auth", "security", "security-review"),
        ("Update the README", "docs", "documentation"),
        ("Compare databases", "research", "inspect-repo"),
        ("Set up CI", "devops", "feature-implementation"),
        ("hello", "other", "inspect-repo"),
    ],
)
def test_select_skill(goal: str, kind: str, expected: str) -> None:
    intent = Intent(goal=goal, kind=kind, risk="low")  # type: ignore[arg-type]
    assert select_skill(intent, REG).meta.name == expected


def test_select_skill_override_and_unknown() -> None:
    intent = Intent(goal="Add a thing", kind="coding", risk="low")
    assert select_skill(intent, REG, "bugfix").meta.name == "bugfix"
    with pytest.raises(ConfigError, match="unknown skill"):
        select_skill(intent, REG, "nope")


def test_plan_is_valid_graph_with_unique_ids_each_call() -> None:
    a, b = plan("Add x"), plan("Add x")
    assert {t.id for t in a.tasks}.isdisjoint({t.id for t in b.tasks})
