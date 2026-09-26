"""Task prompt rendering (PLAYBOOK Appendix B.2) on top of the context pack (§15.3)."""

from __future__ import annotations

from collections.abc import Sequence
from importlib import resources
from typing import Final

import jinja2

from aix.core.context.facts import ProjectFacts
from aix.core.context.pack import Section, assemble_pack
from aix.domain.context import CompactedContext, Handoff
from aix.domain.tasks import Task

DEFAULT_BUDGET_TOKENS: Final = 24000
_ENV: Final = jinja2.Environment(
    undefined=jinja2.StrictUndefined,
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
)


def _handoff_text(handoffs: Sequence[Handoff]) -> str:
    parts: list[str] = []
    for h in handoffs:
        block = [f"[{h.task_id}]", h.summary]
        if h.files_changed:
            block.append("Files: " + ", ".join(h.files_changed))
        if h.open_issues:
            block.append("Open issues: " + "; ".join(h.open_issues))
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def compacted_text(c: CompactedContext) -> str:
    """Render a compacted log where the handoffs section normally goes."""
    lines = ["(earlier work, compacted)", c.summary]
    for label, items in (
        ("Decisions", c.decisions),
        ("Open questions", c.open_questions),
        ("Known failures", c.known_failures),
        ("Important files", c.important_files),
    ):
        if items:
            lines.append(f"{label}: " + "; ".join(items))
    return "\n".join(lines)


def _facts_text(facts: ProjectFacts | None) -> str:
    if facts is None:
        return ""
    lines: list[str] = []
    if facts.repo.languages:
        lines.append("Languages: " + ", ".join(facts.repo.languages))
    if facts.repo.test_commands:
        lines.append("Test commands: " + ", ".join(facts.repo.test_commands))
    for name, text in facts.conventions.items():
        lines.append(f"--- {name} ---\n{text}")
    return "\n".join(lines)


def render_task_prompt(
    task: Task,
    *,
    handoffs: Sequence[Handoff] = (),
    compacted: CompactedContext | None = None,
    failure_notes: Sequence[str] = (),
    facts: ProjectFacts | None = None,
    skill_instructions: str | None = None,
    budget_tokens: int = DEFAULT_BUDGET_TOKENS,
) -> str:
    """Render the Appendix B.2 prompt for ``task``. Deterministic for fixed inputs.

    Contract: the goal, dependency handoffs (or ``compacted`` when given), previous-attempt
    failure notes and project facts share ``budget_tokens`` in that priority order (bottom
    truncated first, secrets redacted); the frame (role, scope, definition of done,
    prohibitions) and skill instructions are fixed. Failure notes must be control-plane facts,
    never agent prose. The first line is always ``TASK TYPE: <type>``.
    """
    pack = assemble_pack(
        [
            Section("goal", task.goal),
            Section(
                "handoffs", compacted_text(compacted) if compacted else _handoff_text(handoffs)
            ),
            Section("failures", "".join(f"- {n}\n" for n in failure_notes)),
            Section("facts", _facts_text(facts)),
        ],
        budget_tokens=budget_tokens,
    )
    part = {s.name: s.text for s in pack.sections}
    source = resources.files("aix.core.prompts").joinpath("task.j2").read_text(encoding="utf-8")
    checks = [c.value for c in (*task.verification.required, *task.verification.optional)]
    return _ENV.from_string(source).render(
        task_type=task.type.value,
        goal=part.get("goal", ""),
        read_only=not task.file_scope,
        scope=", ".join(task.file_scope),
        handoffs=part.get("handoffs", ""),
        failures=part.get("failures", ""),
        project_facts=part.get("facts", ""),
        skill_instructions=(skill_instructions or "").strip(),
        checks=", ".join(checks),
    )
