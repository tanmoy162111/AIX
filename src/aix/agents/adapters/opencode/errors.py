"""OpenCode failure classification (PLAYBOOK §19.1). Patterns are case-insensitive."""

from __future__ import annotations

import re

from aix.domain.enums import FailureClass

_AUTH = re.compile(
    r"providerautherror|\b401\b|unauthorized|unauthenticated|authentication|invalid api key|"
    r"api key|credentials|not logged in|opencode auth login|\b403\b|forbidden",
    re.IGNORECASE,
)
_RATE = re.compile(
    r"\b429\b|rate[_ ]limit|too many requests|quota|insufficient_quota|resource_exhausted|"
    r"exceeded your current",
    re.IGNORECASE,
)
_CONTEXT = re.compile(
    r"contextoverflow|context length|context window|maximum context|prompt is too long|"
    r"context_length_exceeded|too many tokens",
    re.IGNORECASE,
)
_NETWORK = re.compile(
    r"econnreset|enotfound|etimedout|eai_again|fetch failed|unable to connect|connectionrefused|"
    r"connection (?:refused|reset)|socket|network|\b50[234]\b|overloaded|unavailable",
    re.IGNORECASE,
)


def classify_failure(text: str) -> FailureClass:
    """Map error text (session error message/name and stderr) to a ``FailureClass``."""
    for pattern, failure in (
        (_AUTH, FailureClass.AUTH_FAILURE),
        (_RATE, FailureClass.RATE_LIMITED),
        (_CONTEXT, FailureClass.CONTEXT_FAILURE),
        (_NETWORK, FailureClass.NETWORK_FAILURE),
    ):
        if pattern.search(text):
            return failure
    return FailureClass.AGENT_FAILURE
