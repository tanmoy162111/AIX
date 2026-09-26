"""``ai_review`` check (PLAYBOOK §17.3, Appendix B.3): an independent agent reviews the diff.

AI review is evidence *about* the diff, recorded with the reviewer identity. It never changes any
executed check, and an unparseable answer is an ``error``, not a pass.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Collection, Sequence
from importlib import resources
from typing import Final, Literal

import jinja2
from pydantic import Field, ValidationError

from aix.config.schema import RoutingConfig
from aix.core.router.rules import RoutingContext, route
from aix.domain.agents import AgentSpec
from aix.domain.base import DomainModel
from aix.domain.enums import Capability, CheckKind, TaskType
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Task
from aix.domain.verification import Check
from aix.verification.parsers import Finding, Findings

ReviewRunner = Callable[[str], Awaitable[str]]
"""Runs the reviewer agent on a prompt and returns its final message."""

MIN_CONFIDENCE: Final = 0.6
"""A high/critical finding fails the check only at or above this confidence (§17.3)."""
MAX_DIFF_CHARS: Final = 60_000
_FENCE: Final = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


class ReviewParseError(ValueError):
    """The reviewer's reply had no valid findings block."""


class ReviewFinding(DomainModel):
    """One finding from the reviewer (Appendix B.3 schema)."""

    severity: Literal["info", "low", "medium", "high", "critical"]
    file: str | None = None
    line: int | None = None
    title: str = Field(min_length=1)
    detail: str
    confidence: float = Field(ge=0.0, le=1.0)


def parse_review_findings(text: str) -> list[ReviewFinding]:
    """Parse the last fenced JSON block (or last bare object) with a ``findings`` list.

    Raises:
        ReviewParseError: no such block, invalid JSON, or a finding violating the schema.
    """
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    if not candidates:
        start = text.rfind('{"findings"')
        candidates = [text[start:]] if start != -1 else []
    for raw in reversed(candidates):
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        if isinstance(doc, dict) and "findings" in doc:
            items = doc["findings"]  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(items, list):
                raise ReviewParseError("'findings' must be a list")
            try:
                return [ReviewFinding.model_validate(i) for i in items]  # pyright: ignore[reportUnknownVariableType]
            except ValidationError as exc:
                raise ReviewParseError(f"invalid finding: {exc.errors()[0]['msg']}") from exc
    raise ReviewParseError("no JSON block with a 'findings' list in the reply")


def render_review_prompt(
    goal: str, diff: str, checks: Sequence[Check], *, max_diff_chars: int = MAX_DIFF_CHARS
) -> str:
    """Render Appendix B.3. Deterministic for fixed inputs (snapshot-tested)."""
    source = resources.files("aix.core.prompts").joinpath("review.j2").read_text(encoding="utf-8")
    env = jinja2.Environment(
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
    )
    truncated = len(diff) > max_diff_chars
    return env.from_string(source).render(
        goal=goal,
        diff=(diff[:max_diff_chars] if truncated else diff).rstrip("\n"),
        truncated=truncated,
        max_chars=max_diff_chars,
        checks=[{"kind": c.kind.value, "status": c.status, "summary": c.summary} for c in checks],
    )


def pick_reviewer(
    agents: Sequence[AgentSpec], authors: Collection[str], routing: RoutingConfig
) -> str:
    """Route a ``review`` task to the best agent, penalizing the change's authors (§12).

    Raises:
        NoEligibleAgent: nobody can review.
    """
    task = Task(
        id=new_id(IdPrefix.TASK),
        run_id=new_id(IdPrefix.RUN),
        title="ai review",
        goal="review",
        type=TaskType.REVIEW,
        skill="code-review",
        required_capabilities=[Capability.REVIEW],
    )
    decision = route(
        RoutingContext(task=task, agents=agents, authored_by=frozenset(authors), config=routing)
    )
    return decision.primary


def _check(
    status: Literal["passed", "failed", "warning", "error"], summary: str, **kw: object
) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=CheckKind.AI_REVIEW,
        status=status,
        summary=summary,
        **kw,  # type: ignore[arg-type]
    )


async def run_ai_review(
    goal: str,
    diff: str,
    checks: Sequence[Check],
    runner: ReviewRunner,
    *,
    reviewer_id: str,
    required: bool = False,
    min_confidence: float = MIN_CONFIDENCE,
) -> Check:
    """Ask ``runner`` to review ``diff`` and turn the findings into a check.

    Contract: ``failed`` if any high/critical finding has confidence >= ``min_confidence``;
    ``warning`` for any other finding; ``passed`` with none; ``error`` when the runner raises or
    the reply has no valid findings block. The summary names the reviewer.
    """
    prompt = render_review_prompt(goal, diff, checks)
    try:
        reply = await runner(prompt)
    except Exception as exc:
        return _check("error", f"review by {reviewer_id} failed: {exc}", required=required)
    try:
        found = parse_review_findings(reply)
    except ReviewParseError as exc:
        return _check("error", f"unparseable review from {reviewer_id}: {exc}", required=required)
    findings = Findings([Finding(f.severity, f.title, f.detail, f.file, f.line) for f in found])
    if not found:
        return _check("passed", f"reviewed by {reviewer_id}: no findings", required=required)
    blocking = any(
        f.severity in ("high", "critical") and f.confidence >= min_confidence for f in found
    )
    worst = findings.worst()
    top = "; ".join(f"{f.severity} {f.title}" for f in found[:3])
    return _check(
        "failed" if blocking else "warning",
        f"reviewed by {reviewer_id}: {len(found)} finding(s): {top}",
        required=required,
        severity=(worst or "info") if worst != "info" else "info",
        metrics=findings.counts(),
    )
