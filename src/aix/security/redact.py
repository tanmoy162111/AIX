"""Secret redaction (PLAYBOOK §20.5): applied to events, artifacts and captured output."""

from __future__ import annotations

import re
from typing import Final

REDACTED: Final = "[REDACTED]"

SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    (
        "generic-secret",
        re.compile(
            r"""(?ix)\b(?:api[_-]?key|secret(?:[_-]?key)?|token|password|passwd)["']?\s*[:=]\s*
            ["'](?P<v>[^"'\s]{16,})["']"""
        ),
    ),
)


def redact_secrets(text: str) -> str:
    """Replace secret-shaped substrings with ``[REDACTED]``.

    For the generic ``key = "value"`` pattern only the value is replaced; other patterns replace
    the whole match. Text without secrets is returned unchanged.
    """

    def sub(m: re.Match[str]) -> str:
        if "v" in m.re.groupindex and m.group("v") is not None:
            start, end = m.span("v")
            return m.group(0)[: start - m.start()] + REDACTED + m.group(0)[end - m.start() :]
        return REDACTED

    for _rule, pattern in SECRET_PATTERNS:
        text = pattern.sub(sub, text)
    return text
