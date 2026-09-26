"""Failure classification (PLAYBOOK §19.1): adapters first, then generic rules.

Every failed attempt maps to exactly one :class:`~aix.domain.enums.FailureClass` (plus a
verification sub-kind). With Jev enabled, ``failure_triage`` may only refine *within*
:func:`candidates_for`, the classes consistent with the evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from aix.domain.enums import CheckKind as K
from aix.domain.enums import FailureClass as F
from aix.domain.enums import VerificationFailureKind as V
from aix.domain.verification import VerificationReport


@dataclass(frozen=True)
class Classified:
    """A failure class with the optional verification sub-kind."""

    failure: F
    sub_kind: V | None = None

    @property
    def label(self) -> str:
        """Control-plane label such as ``verification_failure:tests`` (safe for decision state)."""
        return (
            f"{self.failure.value}:{self.sub_kind.value}" if self.sub_kind else self.failure.value
        )


# Highest priority first: what to blame when several required checks failed.
_PRIORITY: Final[tuple[tuple[K, F, V | None], ...]] = (
    (K.POLICY, F.POLICY_FAILURE, None),
    (K.SECRETS, F.VERIFICATION_FAILURE, V.SECURITY),
    (K.BUILD, F.VERIFICATION_FAILURE, V.BUILD),
    (K.TESTS, F.VERIFICATION_FAILURE, V.TESTS),
    (K.TYPECHECK, F.VERIFICATION_FAILURE, V.TYPECHECK),
    (K.LINT, F.VERIFICATION_FAILURE, V.LINT),
    (K.SECURITY_SAST, F.VERIFICATION_FAILURE, V.SECURITY),
    (K.DEPS, F.VERIFICATION_FAILURE, V.SECURITY),
    (K.AI_REVIEW, F.VERIFICATION_FAILURE, V.REVIEW),
    (K.CUSTOM, F.VERIFICATION_FAILURE, V.TESTS),
)


def classify_verification(report: VerificationReport) -> Classified | None:
    """The failure a verification report represents, or ``None`` if it does not fail the attempt.

    Only *required* checks count. A failed/error check is classified by kind in priority order
    (policy, secrets, build, tests, typecheck, lint, security, review). A required check that
    could not run (``incomplete``) is a ``TOOL_FAILURE`` (the environment lacks a tool).
    """
    failing = {c.kind for c in report.checks if c.required and c.status in ("failed", "error")}
    for kind, failure, sub in _PRIORITY:
        if kind in failing:
            return Classified(failure, sub)
    if report.overall == "incomplete":
        return Classified(F.TOOL_FAILURE)
    return None


_RATE = r"\b429\b|rate[_ ]?limit|too many requests|quota"
_AUTH = r"\b401\b|\b403\b|unauthori[sz]ed|api key|not logged in|credentials"
_CONTEXT = r"context (?:window|length)|maximum context|prompt is too long"
_RESOURCE = r"no space left|out of memory|disk quota|too many open files"
_NETWORK = r"econnreset|enotfound|etimedout|connection (?:refused|reset)|dns|network"
_GENERIC: Final[tuple[tuple[re.Pattern[str], F], ...]] = tuple(
    (re.compile(pattern, re.IGNORECASE), failure)
    for pattern, failure in (
        (_RATE, F.RATE_LIMITED),
        (_AUTH, F.AUTH_FAILURE),
        (_CONTEXT, F.CONTEXT_FAILURE),
        (_RESOURCE, F.RESOURCE_FAILURE),
        (_NETWORK, F.NETWORK_FAILURE),
    )
)


def classify_attempt(*, agent_failure: F | None, agent_failed: bool, stderr: str) -> F | None:
    """Failure class of the agent process itself.

    ``agent_failure`` is what the adapter already decided (from stream/stderr patterns tested with
    recordings) and always wins. Only for a failed process without one, generic patterns over
    ``stderr`` apply, falling back to ``AGENT_FAILURE``. A process that did not fail has none.
    """
    if not agent_failed:
        return None
    if agent_failure is not None:
        return agent_failure
    for pattern, failure in _GENERIC:
        if pattern.search(stderr):
            return failure
    return F.AGENT_FAILURE


_RELATED: Final[dict[F, tuple[F, ...]]] = {
    F.AGENT_FAILURE: (F.TOOL_FAILURE,),
    F.TIMEOUT: (F.RESOURCE_FAILURE,),
    F.RATE_LIMITED: (F.NETWORK_FAILURE,),
    F.NETWORK_FAILURE: (F.RATE_LIMITED,),
}


def candidates_for(failure: F) -> list[F]:
    """Classes ``failure_triage`` may choose among: the rule class first, then near neighbours."""
    return [failure, *_RELATED.get(failure, ())]
