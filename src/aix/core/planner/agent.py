"""AgentPlanner (PLAYBOOK §13.2): an agent proposes the graph; we validate, repair, or fall back."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Literal

import anyio
import jinja2
from pydantic import ValidationError

from aix.agents.protocol import AgentAdapter, AgentPermissions, AgentRequest
from aix.core.planner.base import Planner
from aix.core.planner.draft import PlanDraft
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.enums import Capability, TaskType
from aix.domain.errors import AgentFailure, ToolFailure
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, RepoFacts, Task, TaskGraph, VerificationSpec
from aix.skills.registry import SkillRegistry
from aix.tools.git import git

PlannerRunner = Callable[[str], Awaitable[str]]
"""Runs the planning agent on a prompt and returns its final message (the claim)."""

MAX_REPAIRS = 2


class PlanInvalid(ValueError):
    """The agent's answer is not a valid plan; ``problems`` are fed back verbatim for repair."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class PlanOutcome:
    """Result of planning plus how it came about (for events and diagnostics)."""

    graph: TaskGraph
    source: Literal["agent", "template_fallback"]
    warnings: list[str] = field(default_factory=list[str])
    errors: list[str] = field(default_factory=list[str])
    """Every problem seen on the way: validation errors and agent failures."""


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object in ``text`` (preferring one with a ``tasks`` key).

    Scans left to right; nested objects are skipped because a decoded object consumes its span.
    Returns ``None`` when the text has no JSON object.
    """
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    i = 0
    while (i := text.find("{", i)) != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)  # pyright: ignore[reportUnknownArgumentType]
        i = end
    with_tasks = [o for o in found if "tasks" in o]
    if with_tasks:
        return with_tasks[-1]
    return found[-1] if found else None


def _format(err: ValidationError) -> list[str]:
    return [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}".strip(": ") for e in err.errors()]


def parse_plan(
    text: str, intent: Intent, run_id: str, registry: SkillRegistry
) -> tuple[TaskGraph, list[str]]:
    """Validate an agent answer and convert it to a :class:`TaskGraph`.

    Contract: returns the graph and warnings (unknown capabilities dropped). Task ids are freshly
    generated; dependencies are rewritten from keys to ids.

    Raises:
        PlanInvalid: no JSON object, schema violation, graph-invariant violation or unknown skill.
    """
    doc = extract_json_object(text)
    if doc is None:
        raise PlanInvalid(["no JSON object found in the reply"])
    try:
        draft = PlanDraft.model_validate(doc)
    except ValidationError as exc:
        raise PlanInvalid(_format(exc)) from exc
    unknown_skills = [
        f"task {t.key!r}: unknown skill {t.skill!r}"
        for t in draft.tasks
        if t.skill is not None and t.skill not in registry.names()
    ]
    if unknown_skills:
        raise PlanInvalid(unknown_skills)

    known = {c.value for c in Capability}
    ids = {t.key: new_id(IdPrefix.TASK) for t in draft.tasks}
    warnings: list[str] = []
    tasks: list[Task] = []
    for t in draft.tasks:
        caps: list[Capability] = []
        for name in t.required_capabilities:
            if name in known:
                caps.append(Capability(name))
            else:
                warnings.append(f"task {t.key!r}: unknown capability {name!r} dropped")
        tasks.append(
            Task(
                id=ids[t.key],
                run_id=run_id,
                title=t.title,
                goal=t.goal,
                type=t.type,
                skill=t.skill,
                required_capabilities=caps,
                depends_on=[ids[d] for d in t.depends_on],
                file_scope=list(t.file_scope),
                verification=VerificationSpec(
                    required=list(t.verification.required), optional=list(t.verification.optional)
                ),
                risk=t.risk or intent.risk,
            )
        )
    try:
        return TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=tasks), warnings
    except ValidationError as exc:  # pragma: no cover - PlanDraft already enforces the invariants
        raise PlanInvalid(_format(exc)) from exc


def render_planner_prompt(
    intent: Intent, repo_facts: RepoFacts, registry: SkillRegistry, *, max_tasks: int
) -> str:
    """Render Appendix B.1. Deterministic for fixed inputs (snapshot-tested)."""
    source = resources.files("aix.core.prompts").joinpath("planner.j2").read_text(encoding="utf-8")
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    return env.from_string(source).render(
        goal=intent.goal,
        kind=intent.kind,
        risk=intent.risk,
        target_paths=intent.target_paths,
        languages=repo_facts.languages,
        package_managers=repo_facts.package_managers,
        test_commands=repo_facts.test_commands,
        convention_files=repo_facts.convention_files,
        file_count=repo_facts.file_count,
        total_bytes=repo_facts.total_bytes,
        task_types=[t.value for t in TaskType],
        capabilities=[c.value for c in Capability],
        skills=[
            {
                "name": s.meta.name,
                "description": s.meta.description,
                "task_types": [t.value for t in s.meta.task_types],
            }
            for s in registry.all()
        ],
        max_tasks=max_tasks,
        schema=json.dumps(PlanDraft.model_json_schema(), indent=2, sort_keys=True),
    )


class AgentPlanner:
    """Asks an agent for a plan, re-prompts on validation errors, then falls back."""

    def __init__(
        self,
        registry: SkillRegistry,
        runner: PlannerRunner,
        fallback: Planner,
        *,
        max_tasks: int = 12,
    ) -> None:
        self._registry = registry
        self._runner = runner
        self._fallback = fallback
        self._max_tasks = max_tasks

    async def plan(
        self, intent: Intent, repo_facts: RepoFacts, run_id: str, skill: str | None = None
    ) -> TaskGraph:
        """Planner protocol: the graph only. See :meth:`plan_with_report` for diagnostics."""
        return (await self.plan_with_report(intent, repo_facts, run_id, skill)).graph

    async def plan_with_report(
        self, intent: Intent, repo_facts: RepoFacts, run_id: str, skill: str | None = None
    ) -> PlanOutcome:
        """Plan with the agent; never raises for agent or validation problems.

        Contract: at most ``1 + MAX_REPAIRS`` agent calls. Each repair prompt is the previous
        prompt plus the validation errors verbatim. An agent failure skips repair. After the last
        failure the deterministic fallback planner produces the graph.
        """
        prompt = render_planner_prompt(
            intent, repo_facts, self._registry, max_tasks=self._max_tasks
        )
        errors: list[str] = []
        for attempt in range(1 + MAX_REPAIRS):
            try:
                reply = await self._runner(prompt)
            except Exception as exc:
                errors.append(f"planner agent failed: {exc}")
                break
            try:
                graph, warnings = parse_plan(reply, intent, run_id, self._registry)
            except PlanInvalid as exc:
                errors.extend(exc.problems)
                if attempt < MAX_REPAIRS:
                    listing = "\n".join(f"- {p}" for p in exc.problems)
                    prompt = (
                        f"{prompt}\n\nYOUR PREVIOUS REPLY WAS INVALID\n{listing}\n"
                        "Reply again with a single corrected JSON object and nothing after it.\n"
                    )
                continue
            return PlanOutcome(graph=graph, source="agent", warnings=warnings, errors=errors)
        graph = await self._fallback.plan(intent, repo_facts, run_id, skill)
        return PlanOutcome(graph=graph, source="template_fallback", errors=errors)


def make_adapter_runner(
    adapter: AgentAdapter,
    wm: WorkspaceManager,
    run_id: str,
    *,
    timeout_s: int,
    model: str | None = None,
) -> PlannerRunner:
    """Build a runner that executes ``adapter`` read-only in a throwaway worktree (§13.2).

    Contract: the worktree and its temporary branch are always removed. A non-completed outcome or
    any modified file raises (``AgentFailure`` / ``ToolFailure``) so the planner falls back.
    """

    async def run(prompt: str) -> str:
        attempt_id = new_id(IdPrefix.ATTEMPT)
        ws = await wm.create_attempt_workspace(run_id, attempt_id)
        try:
            handle = await adapter.start(
                AgentRequest(
                    attempt_id=attempt_id,
                    workspace=ws.path,
                    prompt=prompt,
                    model=model,
                    timeout_s=timeout_s,
                    permissions=AgentPermissions(read_only=True),
                )
            )
            try:
                async for _ in adapter.events(handle):
                    pass
                outcome = await adapter.wait(handle)
            except BaseException:
                with anyio.CancelScope(shield=True):
                    await adapter.cancel(handle, 1)
                raise
            if outcome.status != "completed":
                raise AgentFailure(
                    f"planner agent {outcome.status}: {outcome.stderr_tail[-200:].strip()}"
                )
            changed = (await wm.capture_diff(ws)).summary.paths
            if changed:
                raise ToolFailure(f"planner agent modified files: {', '.join(changed[:5])}")
            return outcome.claim or ""
        finally:
            with anyio.CancelScope(shield=True):
                await wm.remove_workspace(ws)
                await git(wm.root, "branch", "-D", ws.branch, check=False)

    return run
