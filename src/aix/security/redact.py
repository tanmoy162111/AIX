"""Secret redaction (PLAYBOOK §20.5): applied to events, artifacts and captured output."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
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


_SECRET_NAME: Final = re.compile(
    r"(?i)(api[_-]?key|token|secret|passw(?:or)?d|credential|private[_-]?key|auth)"
)
MIN_SECRET_LEN: Final = 8
_known: tuple[str, ...] = ()


def known_secret_values(env: Mapping[str, str]) -> list[str]:
    """Values of environment variables that look like credentials (by name), longest first.

    Values shorter than ``MIN_SECRET_LEN`` are ignored: replacing a short common string would
    mangle unrelated text.
    """
    values = {v for k, v in env.items() if _SECRET_NAME.search(k) and len(v) >= MIN_SECRET_LEN}
    return sorted(values, key=len, reverse=True)


def configure_known_secrets(values: Iterable[str]) -> None:
    """Register literal secret values that :func:`redact_secrets` must always remove.

    Process-wide by design (ADR-0028): redaction is applied in many places that cannot all be
    handed a redactor. Called once at startup; calling again replaces the set.
    """
    global _known
    _known = tuple(sorted({v for v in values if len(v) >= MIN_SECRET_LEN}, key=len, reverse=True))


def add_known_secrets(values: Iterable[str]) -> None:
    """Add to the registered secret values (see :func:`configure_known_secrets`)."""
    configure_known_secrets([*_known, *values])


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
    for value in _known:
        if value in text:
            text = text.replace(value, REDACTED)
    return text
