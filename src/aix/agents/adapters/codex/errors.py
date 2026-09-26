"""Codex CLI failure classification (PLAYBOOK §19.1). Patterns are case-insensitive."""

from __future__ import annotations

import re

from aix.domain.enums import FailureClass

_AUTH = re.compile(
    r"\b401\b|unauthorized|not logged in|codex login|incorrect api key|invalid api key|"
    r"refresh token|authentication",
    re.IGNORECASE,
)
_RATE = re.compile(
    r"\b429\b|rate[_ ]limit|too many requests|quota|usage limit|insufficient_quota", re.IGNORECASE
)
_NETWORK = re.compile(
    r"stream disconnected|error sending request|dns error|failed to lookup|"
    r"connection (?:refused|reset)|timed out|econnreset|enotfound",
    re.IGNORECASE,
)
_CONTEXT = re.compile(
    r"context_length_exceeded|context window|maximum context length", re.IGNORECASE
)


def classify_failure(text: str) -> FailureClass:
    """Map error text (stream errors and stderr) to a ``FailureClass``."""
    for pattern, failure in (
        (_AUTH, FailureClass.AUTH_FAILURE),
        (_RATE, FailureClass.RATE_LIMITED),
        (_CONTEXT, FailureClass.CONTEXT_FAILURE),
        (_NETWORK, FailureClass.NETWORK_FAILURE),
    ):
        if pattern.search(text):
            return failure
    return FailureClass.AGENT_FAILURE
