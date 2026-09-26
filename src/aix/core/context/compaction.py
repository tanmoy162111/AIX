"""Compaction of the handoff/decision log (PLAYBOOK §15.4)."""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Final

from aix.core.context.handoff import CLAIM_LABEL
from aix.core.context.pack import estimate_tokens
from aix.domain.context import CompactedContext, Handoff
from aix.security.redact import redact_secrets

MAX_FILES: Final = 20
Summarizer = Callable[[str], Awaitable[str]]
"""Turns the deterministic digest into prose (a ``summarize``-capable agent); may raise."""


@dataclass(frozen=True)
class Compaction:
    result: CompactedContext
    before_sha256: str
    after_sha256: str


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _facts_only(summary: str) -> str:
    """Drop the quoted agent claim: compaction keeps facts, never unverified prose."""
    return summary.split(f"\n\n{CLAIM_LABEL}", 1)[0]


def _log_text(
    handoffs: Sequence[Handoff], decisions: Sequence[str], failures: Sequence[str]
) -> str:
    parts = [f"[{h.task_id}]\n{h.summary}\n{h.files_changed}\n{h.open_issues}" for h in handoffs]
    return "\n\n".join([*parts, *decisions, *failures])


def _digest(handoffs: Sequence[Handoff]) -> str:
    lines: list[str] = []
    for h in handoffs:
        first, *rest = _facts_only(h.summary).splitlines()
        checks = next((ln for ln in rest if ln.startswith("Checks:")), "")
        lines.append(f"{h.task_id}: {first} | {checks}".rstrip(" |"))
    return "\n".join(lines)


async def compact_context(
    handoffs: Sequence[Handoff],
    decisions: Sequence[str] = (),
    failures: Sequence[str] = (),
    *,
    budget_tokens: int,
    summarizer: Summarizer | None = None,
) -> Compaction | None:
    """Compact the log when its estimated size exceeds ``budget_tokens``; else return ``None``.

    Contract: deterministic given the inputs and no ``summarizer``. Agent claims are dropped;
    open issues become ``open_questions``; failures are de-duplicated in first-seen order;
    ``important_files`` are the most-touched paths (ties broken alphabetically, at most
    ``MAX_FILES``). A ``summarizer`` replaces ``summary`` with its prose; if it raises or returns
    nothing, the deterministic digest is used. ``before_sha256`` and ``after_sha256`` hash the
    original log and the compacted result, for the ``context.compacted`` event.
    """
    before = _log_text(handoffs, decisions, failures)
    if estimate_tokens(before) <= budget_tokens:
        return None
    digest = _digest(handoffs)
    summary = digest
    if summarizer is not None:
        try:
            summary = (await summarizer(digest)).strip() or digest
        except Exception:  # any summarizer failure must not block the run
            summary = digest
    counts = Counter(f for h in handoffs for f in h.files_changed)
    ranked = sorted(counts, key=lambda f: (-counts[f], f))[:MAX_FILES]
    result = CompactedContext(
        summary=redact_secrets(summary),
        decisions=list(decisions),
        open_questions=list(dict.fromkeys(i for h in handoffs for i in h.open_issues)),
        known_failures=list(dict.fromkeys(failures)),
        important_files=ranked,
    )
    return Compaction(result, _sha(before), _sha(result.model_dump_json()))
