"""Plan post-processing shared by both planners (PLAYBOOK §13.3, §14.3)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from pydantic import ValidationError

from aix.domain.enums import Capability, CheckKind, TaskType
from aix.domain.tasks import Task, TaskGraph, VerificationSpec
from aix.skills.registry import SkillRegistry

_TYPE_CAPABILITY: Final[dict[TaskType, Capability]] = {
    TaskType.INSPECT: Capability.RESEARCH,
    TaskType.RESEARCH: Capability.RESEARCH,
    TaskType.DESIGN: Capability.DESIGN,
    TaskType.IMPLEMENT: Capability.IMPLEMENT,
    TaskType.TEST: Capability.TEST,
    TaskType.REVIEW: Capability.REVIEW,
    TaskType.SECURITY_REVIEW: Capability.SECURITY,
    TaskType.DOCUMENT: Capability.DOCUMENT,
    TaskType.INTEGRATE: Capability.IMPLEMENT,
}
_WILDCARDS: Final = frozenset("*?[")


def _split(glob: str) -> tuple[list[str], list[str]]:
    """Split a glob into its literal directory prefix and the components from the first wildcard."""
    parts = [p for p in glob.strip("/").split("/") if p and p != "."]
    for i, part in enumerate(parts):
        if _WILDCARDS & set(part):
            return parts[:i], parts[i:]
    return parts[:-1], parts[-1:]  # a literal path: the last component is the file name


def _literal(tail: list[str]) -> bool:
    return len(tail) == 1 and not (_WILDCARDS & set(tail[0]))


def globs_overlap(a: str, b: str) -> bool:
    """Conservatively decide whether two ``file_scope`` globs may match a common path.

    Contract: ``False`` only when the globs provably cannot overlap (different literal
    subtrees, or a shallow pattern without ``**`` that cannot reach into the other's directory).
    Symmetric.
    """
    (pa, ta), (pb, tb) = _split(a), _split(b)
    short, long_ = (pa, pb) if len(pa) <= len(pb) else (pb, pa)
    if long_[: len(short)] != short:
        return False
    if pa == pb:
        # same directory: overlap unless both are exact, different file names
        return not (_literal(ta) and _literal(tb) and ta != tb)
    # one prefix strictly contains the other: the shallower glob must be able to reach deeper
    shallow_tail, shallow_prefix = (ta, pa) if len(pa) < len(pb) else (tb, pb)
    deep_prefix = pb if len(pa) < len(pb) else pa
    recursive = "**" in shallow_tail
    depth = len(shallow_prefix) + len(shallow_tail)
    return recursive or depth > len(deep_prefix)


def _is_write(task: Task) -> bool:
    return bool(task.file_scope)


def _scopes_overlap(a: Task, b: Task) -> bool:
    return any(globs_overlap(x, y) for x in a.file_scope for y in b.file_scope)


def _ancestors(tasks: Sequence[Task]) -> dict[str, set[str]]:
    by_id = {t.id: t for t in tasks}
    memo: dict[str, set[str]] = {}

    def visit(task_id: str) -> set[str]:
        if task_id not in memo:
            out: set[str] = set()
            for dep in by_id[task_id].depends_on:
                out.add(dep)
                out |= visit(dep)
            memo[task_id] = out
        return memo[task_id]

    return {t.id: visit(t.id) for t in tasks}


def _add_implicit_edges(tasks: list[Task], warnings: list[str]) -> list[Task]:
    """Order overlapping, unordered write tasks: the earlier-listed one goes first (§14.3)."""
    result = list(tasks)
    for j in range(len(result)):
        for i in range(j):
            a, b = result[i], result[j]
            if not (_is_write(a) and _is_write(b)) or not _scopes_overlap(a, b):
                continue
            anc = _ancestors(result)
            if a.id in anc[b.id] or b.id in anc[a.id]:
                continue
            result[j] = b.model_copy(update={"depends_on": [*b.depends_on, a.id]})
            warnings.append(
                f"added edge {a.title!r} -> {b.title!r}: overlapping file_scope (§14.3)"
            )
    return result


def _union[T](*groups: Sequence[T]) -> list[T]:
    return list(dict.fromkeys(item for group in groups for item in group))


def _apply_defaults(
    task: Task, required_default: Sequence[CheckKind], registry: SkillRegistry
) -> Task:
    caps = _union(task.required_capabilities) or [_TYPE_CAPABILITY[task.type]]
    update: dict[str, object] = {"required_capabilities": caps}
    if _is_write(task):
        skill = registry.get(task.skill) if task.skill in registry.names() and task.skill else None
        skill_required = skill.verification.required if skill else []
        skill_optional = skill.verification.optional if skill else []
        required = _union(task.verification.required, skill_required, required_default)
        optional = [
            c for c in _union(task.verification.optional, skill_optional) if c not in required
        ]
        update["verification"] = VerificationSpec(required=required, optional=optional)
    return task.model_copy(update=update)


def _topo_index(tasks: Sequence[Task]) -> dict[str, int]:
    graph = TaskGraph.model_construct(id="", run_id="", tasks=list(tasks))
    return {t.id: i for i, t in enumerate(graph.topological_order())}


def _merge(parent: Task, child: Task) -> Task:
    return parent.model_copy(
        update={
            "goal": f"{parent.goal}\n\nAlso ({child.title}): {child.goal}",
            "file_scope": _union(parent.file_scope, child.file_scope),
            "required_capabilities": _union(
                parent.required_capabilities, child.required_capabilities
            ),
            "depends_on": [
                d for d in _union(parent.depends_on, child.depends_on) if d != parent.id
            ],
            "verification": VerificationSpec(
                required=_union(parent.verification.required, child.verification.required),
                optional=_union(parent.verification.optional, child.verification.optional),
            ),
        }
    )


def _try_merge(graph_id: str, tasks: list[Task], child: Task, parent: Task) -> list[Task] | None:
    merged: list[Task] = []
    for t in tasks:
        if t.id == child.id:
            continue
        if t.id == parent.id:
            t = _merge(t, child)
        elif child.id in t.depends_on:
            t = t.model_copy(
                update={
                    "depends_on": _union([parent.id if d == child.id else d for d in t.depends_on])
                }
            )
        merged.append(t)
    try:
        TaskGraph(id=graph_id, run_id=child.run_id, tasks=merged)
    except (ValidationError, ValueError):
        return None
    return merged


def _cap(graph_id: str, tasks: list[Task], max_tasks: int, warnings: list[str]) -> list[Task]:
    while len(tasks) > max_tasks:
        order = _topo_index(tasks)
        child = max(tasks, key=lambda t: order[t.id])
        by_id = {t.id: t for t in tasks}
        candidates = sorted(
            (by_id[d] for d in child.depends_on),
            key=lambda t: order[t.id],
            reverse=True,
        )
        candidates += sorted(
            (t for t in tasks if t.id != child.id and t.id not in child.depends_on),
            key=lambda t: order[t.id],
            reverse=True,
        )
        for parent in candidates:
            merged = _try_merge(graph_id, tasks, child, parent)
            if merged is not None:
                warnings.append(f"merged task {child.title!r} into {parent.title!r} (max_tasks)")
                tasks = merged
                break
        else:
            warnings.append(f"cannot reduce plan below {len(tasks)} tasks without a cycle")
            break
    return tasks


def postprocess_plan(
    graph: TaskGraph,
    *,
    max_tasks: int,
    required_default: Sequence[CheckKind],
    registry: SkillRegistry,
) -> tuple[TaskGraph, list[str]]:
    """Apply §13.3 to a planner's graph and return it with human-readable warnings.

    Order: cap at ``max_tasks`` (merge excess into parents), add implicit edges between
    overlapping unordered write tasks, then fill defaults: capabilities derived from the task
    type when none are given, and for write tasks ``verification`` = task spec + skill spec +
    ``required_default``. Read-only tasks get no verification. The result is re-validated.
    """
    warnings: list[str] = []
    tasks = _cap(graph.id, list(graph.tasks), max_tasks, warnings)
    tasks = _add_implicit_edges(tasks, warnings)
    tasks = [_apply_defaults(t, required_default, registry) for t in tasks]
    return TaskGraph(id=graph.id, run_id=graph.run_id, tasks=tasks), warnings
