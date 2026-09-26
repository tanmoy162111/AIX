"""Context pack assembly with a token budget (PLAYBOOK §15.3, ADR-0023)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from aix.security.redact import redact_secrets

TRUNCATION_MARK: Final = "[truncated]"
CHARS_PER_TOKEN: Final = 4


def estimate_tokens(text: str) -> int:
    """Tokenizer-free estimate: ``ceil(chars / 4)`` (ADR-0023)."""
    return -(-len(text) // CHARS_PER_TOKEN)


@dataclass(frozen=True)
class Section:
    """One named block of context; callers pass sections highest priority first."""

    name: str
    text: str


@dataclass(frozen=True)
class ContextPack:
    sections: list[Section]
    truncated: list[str]
    dropped: list[str]
    tokens: int

    @property
    def text(self) -> str:
        return "\n\n".join(f"{s.name.upper()}\n{s.text}" for s in self.sections)


def _cut(text: str, max_tokens: int) -> str:
    """Longest whole-line prefix of ``text`` that, with the marker, fits ``max_tokens``."""
    room = max(max_tokens * CHARS_PER_TOKEN - len(TRUNCATION_MARK) - 1, 0)
    head = text[:room]
    if len(text) > room and "\n" in head:
        head = head[: head.rindex("\n")]
    return f"{head}\n{TRUNCATION_MARK}" if head else TRUNCATION_MARK


def assemble_pack(sections: Sequence[Section], *, budget_tokens: int) -> ContextPack:
    """Fill ``budget_tokens`` with ``sections`` in the given (priority) order.

    Contract: text is secret-redacted; empty sections are skipped. The first non-empty section
    is always kept (cut if it alone is over budget). For the rest, the first section that does
    not fit is cut at a line boundary and every later one is dropped whole, so truncation always
    happens from the bottom. ``tokens`` never exceeds ``budget_tokens`` (section headers
    included in the estimate).
    """
    kept: list[Section] = []
    truncated: list[str] = []
    dropped: list[str] = []
    used = 0
    full = False
    for section in sections:
        text = redact_secrets(section.text.strip())
        if not text:
            continue
        if full:
            dropped.append(section.name)
            continue
        overhead = estimate_tokens(section.name) + 1  # "NAME\n" header
        cost = estimate_tokens(text) + overhead
        if used + cost <= budget_tokens:
            kept.append(Section(section.name, text))
            used += cost
            continue
        room = budget_tokens - used - overhead
        full = True
        if room > 0:
            text = _cut(text, room)
            kept.append(Section(section.name, text))
            truncated.append(section.name)
            used += estimate_tokens(text) + overhead
        else:
            dropped.append(section.name)
    return ContextPack(sections=kept, truncated=truncated, dropped=dropped, tokens=used)
