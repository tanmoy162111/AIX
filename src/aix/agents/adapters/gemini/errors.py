"""Gemini CLI failure classification (PLAYBOOK §19.1). Patterns are case-insensitive."""

from __future__ import annotations

import re

from aix.domain.enums import FailureClass

AUTH_EXIT_CODE = 41
"""``FATAL_AUTHENTICATION_ERROR`` in gemini-cli's exit-code table."""

_AUTH = re.compile(
    r"\b401\b|unauthenticated|api key not valid|api_key_invalid|please set an auth method|"
    r"not authenticated|invalid authentication|gemini_api_key|failed to login|"
    r"permission_denied|\b403\b|error authenticating|ineligibletiererror|unsupported_client",
    re.IGNORECASE,
)
_RATE = re.compile(
    r"\b429\b|resource_exhausted|quota|rate[_ ]limit|too many requests", re.IGNORECASE
)
_CONTEXT = re.compile(
    r"token count exceeds|exceeds the maximum number of tokens|input token count|context window|"
    r"context_length",
    re.IGNORECASE,
)
_NETWORK = re.compile(
    r"fetch failed|econnreset|enotfound|etimedout|eai_again|getaddrinfo|socket hang up|"
    r"network error|\b503\b|unavailable|overloaded|connection (?:refused|reset)",
    re.IGNORECASE,
)


def classify_failure(text: str, exit_code: int | None = None) -> FailureClass:
    """Map error text (result error, stream errors, stderr) and the exit code to a class."""
    if exit_code == AUTH_EXIT_CODE:
        return FailureClass.AUTH_FAILURE
    for pattern, failure in (
        (_AUTH, FailureClass.AUTH_FAILURE),
        (_RATE, FailureClass.RATE_LIMITED),
        (_CONTEXT, FailureClass.CONTEXT_FAILURE),
        (_NETWORK, FailureClass.NETWORK_FAILURE),
    ):
        if pattern.search(text):
            return failure
    return FailureClass.AGENT_FAILURE
