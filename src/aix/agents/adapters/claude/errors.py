"""Claude Code failure classification (PLAYBOOK §19.1). Patterns are case-insensitive."""

from __future__ import annotations

import re

from aix.domain.enums import FailureClass

_AUTH = re.compile(
    r"invalid api key|please run /login|oauth token|authentication[_ ](?:error|failed)|"
    r"unauthorized|\b401\b|credit balance is too low",
    re.IGNORECASE,
)
_RATE = re.compile(
    r"rate[_ ]limit|\b429\b|usage limit|overloaded|\b529\b|too many requests", re.IGNORECASE
)
_NETWORK = re.compile(
    r"econnreset|enotfound|etimedout|econnrefused|network error|fetch failed|socket hang up|"
    r"getaddrinfo",
    re.IGNORECASE,
)
_CONTEXT = re.compile(
    r"prompt is too long|context (?:length|window)|too many tokens", re.IGNORECASE
)


def classify_failure(text: str, subtype: str | None = None) -> FailureClass:
    """Map a result subtype and error text to a ``FailureClass``."""
    if subtype == "error_max_budget_usd":
        return FailureClass.BUDGET_EXCEEDED
    for pattern, failure in (
        (_AUTH, FailureClass.AUTH_FAILURE),
        (_RATE, FailureClass.RATE_LIMITED),
        (_NETWORK, FailureClass.NETWORK_FAILURE),
        (_CONTEXT, FailureClass.CONTEXT_FAILURE),
    ):
        if pattern.search(text):
            return failure
    return FailureClass.AGENT_FAILURE
