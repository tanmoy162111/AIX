from __future__ import annotations

import pytest

from aix.core.planner.postprocess import globs_overlap, postprocess_plan
from aix.domain.enums import Capability, CheckKind, TaskType
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Task, TaskGraph, VerificationSpec
from aix.skills.registry import SkillRegistry

REG = SkillRegistry.builtin()
RUN = new_id(IdPrefix.RUN)
DEFAULT = [CheckKind.BUILD, CheckKind.TESTS, CheckKind.LINT]


def task(
    type_: TaskType = TaskType.IMPLEMENT,
    *,
    title: str = "t",
    deps: list[str] | None = None,
    scope: list[str] | None = None,
    caps: list[Capability] | None = None,
    skill: str | None = None,
    verification: VerificationSpec | None = None,
) -> Task:
    return Task(
        id=new_id(IdPrefix.TASK),
        run_id=RUN,
        title=title,
        goal=f"goal of {title}",
        type=type_,
        skill=skill,
        depends_on=deps or [],
        file_scope=scope or [],
        required_capabilities=caps or [],
        verification=verification or VerificationSpec(),
    )


def graph(*tasks: Task) -> TaskGraph:
    return TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=RUN, tasks=list(tasks))


def post(g: TaskGraph, max_tasks: int = 12) -> tuple[TaskGraph, list[str]]:
    return postprocess_plan(g, max_tasks=max_tasks, required_default=DEFAULT, registry=REG)


# ---- glob overlap --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("**", "src/x.py", True),
        ("src/**", "src/aix/**", True),
        ("src/**", "tests/**", False),
        ("src/a/*.py", "src/b/*.py", False),
        ("src/a.py", "src/a.py", True),
        ("*.md", "docs/**", False),
        ("*.md", "**", True),
        ("**/test_*", "src/**", True),
        ("docs/**", "docs/guide/*.md", True),
        ("src/a.py", "src/b.py", False),
    ],
)
def test_globs_overlap(a: str, b: str, expected: bool) -> None:
    assert globs_overlap(a, b) is expected
    assert globs_overlap(b, a) is expected


# ---- implicit ordering ---------------------------------------------------------------------


def test_overlapping_unordered_writers_get_an_edge_earlier_first() -> None:
    a = task(title="a", scope=["src/**"])
    b = task(title="b", scope=["src/aix/**"])
    out, warnings = post(graph(a, b))
    by_id = {t.id: t for t in out.tasks}
    assert by_id[b.id].depends_on == [a.id] and by_id[a.id].depends_on == []
    assert any("overlapping file_scope" in w for w in warnings)


def test_disjoint_writers_stay_parallel() -> None:
    a, b = task(title="a", scope=["src/**"]), task(title="b", scope=["docs/**"])
    out, _ = post(graph(a, b))
    assert all(t.depends_on == [] for t in out.tasks)


def test_already_ordered_writers_get_no_extra_edge() -> None:
    a = task(title="a", scope=["**"])
    mid = task(TaskType.REVIEW, title="mid", deps=[a.id])
    b = task(title="b", scope=["**"], deps=[mid.id])
    out, warnings = post(graph(a, mid, b))
    assert {t.title: t.depends_on for t in out.tasks}["b"] == [mid.id]
    assert not warnings


def test_read_only_tasks_never_get_edges() -> None:
    out, _ = post(graph(task(TaskType.REVIEW), task(TaskType.REVIEW)))
    assert all(t.depends_on == [] for t in out.tasks)


def test_listing_order_against_dependencies_never_creates_a_cycle() -> None:
    b = task(title="b", scope=["src/**"])
    a = task(title="a", scope=["src/**"], deps=[b.id])
    out, _ = post(graph(a, b))  # a listed first but depends on b: no reverse edge
    assert {t.title: t.depends_on for t in out.tasks} == {"a": [b.id], "b": []}


# ---- verification defaults -----------------------------------------------------------------


def test_write_tasks_get_skill_and_default_checks_read_only_do_not() -> None:
    w = task(
        scope=["src/**"],
        skill="bugfix",
        verification=VerificationSpec(required=[CheckKind.SECRETS]),
    )
    r = task(TaskType.REVIEW)
    out, _ = post(graph(w, r))
    got = {t.id: t for t in out.tasks}
    assert got[w.id].verification.required == [
        CheckKind.SECRETS,
        CheckKind.BUILD,
        CheckKind.TESTS,
        CheckKind.LINT,
    ]
    assert CheckKind.TYPECHECK in got[w.id].verification.optional  # from the bugfix skill
    assert got[r.id].verification == VerificationSpec()


def test_verification_without_skill_still_gets_defaults_and_no_duplicates() -> None:
    w = task(scope=["**"], verification=VerificationSpec(required=[CheckKind.TESTS]))
    out, _ = post(graph(w))
    assert out.tasks[0].verification.required == [CheckKind.TESTS, CheckKind.BUILD, CheckKind.LINT]


def test_optional_excludes_checks_that_became_required() -> None:
    w = task(scope=["**"], verification=VerificationSpec(optional=[CheckKind.LINT]))
    out, _ = post(graph(w))
    assert CheckKind.LINT not in out.tasks[0].verification.optional


# ---- capabilities --------------------------------------------------------------------------


def test_missing_capabilities_are_derived_from_type_and_duplicates_removed() -> None:
    a = task(TaskType.SECURITY_REVIEW)
    b = task(TaskType.IMPLEMENT, scope=["**"], caps=[Capability.DEBUG, Capability.DEBUG])
    out, _ = post(graph(a, b))
    got = {t.id: t for t in out.tasks}
    assert got[a.id].required_capabilities == [Capability.SECURITY]
    assert got[b.id].required_capabilities == [Capability.DEBUG]


# ---- cap -----------------------------------------------------------------------------------


def test_graph_within_cap_is_untouched_apart_from_defaults() -> None:
    a, b = task(TaskType.INSPECT, title="a"), task(TaskType.REVIEW, title="b", deps=[])
    out, _ = post(graph(a, b), max_tasks=2)
    assert [t.title for t in out.tasks] == ["a", "b"]


def test_excess_tasks_are_merged_into_their_parent() -> None:
    a = task(TaskType.INSPECT, title="a")
    b = task(scope=["src/**"], title="b", deps=[a.id])
    c = task(TaskType.REVIEW, title="c", deps=[b.id])
    d = task(TaskType.DOCUMENT, title="d", deps=[c.id], scope=["docs/**"])
    e = task(TaskType.TEST, title="e", deps=[d.id], scope=["tests/**"])
    out, warnings = post(graph(a, b, c, d, e), max_tasks=3)
    assert len(out.tasks) == 3
    assert any("merged" in w for w in warnings)
    ids = {t.id for t in out.tasks}
    assert all(set(t.depends_on) <= ids for t in out.tasks)
    text = " ".join(t.goal for t in out.tasks)
    assert "goal of d" in text and "goal of e" in text  # merged goals are preserved
    scopes = {s for t in out.tasks for s in t.file_scope}
    assert {"docs/**", "tests/**", "src/**"} <= scopes


def test_merge_rewires_dependents_and_stays_acyclic() -> None:
    a = task(TaskType.INSPECT, title="a")
    b = task(TaskType.IMPLEMENT, title="b", deps=[a.id], scope=["src/**"])
    x = task(TaskType.TEST, title="x", deps=[b.id], scope=["tests/**"])
    c = task(TaskType.REVIEW, title="c", deps=[b.id, x.id])  # tricky: two dependencies
    d = task(TaskType.DOCUMENT, title="d", deps=[c.id], scope=["docs/**"])
    out, _ = post(graph(a, b, x, c, d), max_tasks=3)
    assert len(out.tasks) == 3
    assert len(out.topological_order()) == 3  # acyclic (TaskGraph also re-validated)
