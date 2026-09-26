"""Typed errors. Every error maps to exactly one ``FailureClass`` (PLAYBOOK §19.1, CLAUDE.md §7).

Control-plane-internal errors are mapped as follows: ``IllegalTransition`` is a control-plane
defect and maps to ``TOOL_FAILURE``; ``ConfigError`` is invalid configuration or policy and maps
to ``POLICY_FAILURE``; ``UnsupportedError`` (e.g. adapter cannot resume a session) maps to
``AGENT_FAILURE``.
"""

from __future__ import annotations

import errno
from typing import ClassVar

from pydantic import JsonValue

from aix.domain.enums import FailureClass, VerificationFailureKind


class AixError(Exception):
    """Base class. Subclasses must set ``failure_class``."""

    failure_class: ClassVar[FailureClass]

    def __init__(self, message: str = "", *, details: dict[str, JsonValue] | None = None) -> None:
        if not hasattr(type(self), "failure_class"):
            raise TypeError(f"{type(self).__name__} does not declare a failure_class")
        super().__init__(message)
        self.details: dict[str, JsonValue] = dict(details or {})


class AgentFailure(AixError):
    """The agent exited non-zero or crashed."""

    failure_class = FailureClass.AGENT_FAILURE


class AgentNoChanges(AixError):
    """A write task produced an empty diff."""

    failure_class = FailureClass.AGENT_NO_CHANGES


class ToolFailure(AixError):
    """A tool (git, shell, ...) failed."""

    failure_class = FailureClass.TOOL_FAILURE


class NetworkFailure(AixError):
    """A network operation failed."""

    failure_class = FailureClass.NETWORK_FAILURE


class AuthFailure(AixError):
    """The agent or provider rejected our credentials."""

    failure_class = FailureClass.AUTH_FAILURE


class RateLimited(AixError):
    """The provider rate-limited us."""

    failure_class = FailureClass.RATE_LIMITED


class AgentTimeout(AixError):
    """An attempt exceeded its time limit."""

    failure_class = FailureClass.TIMEOUT


class VerificationFailed(AixError):
    """Executed verification did not pass. ``kind`` refines the class."""

    failure_class = FailureClass.VERIFICATION_FAILURE

    def __init__(
        self,
        message: str = "",
        *,
        kind: VerificationFailureKind | None = None,
        details: dict[str, JsonValue] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.kind = kind


class ScopeViolation(AixError):
    """Files changed outside the task's ``file_scope``."""

    failure_class = FailureClass.SCOPE_VIOLATION


class PolicyViolation(AixError):
    """An action was forbidden by policy."""

    failure_class = FailureClass.POLICY_FAILURE


class MergeConflict(AixError):
    """Integrating an attempt into the run branch conflicted."""

    failure_class = FailureClass.MERGE_CONFLICT


class ContextFailure(AixError):
    """Prompt too large or a dependency's output is missing."""

    failure_class = FailureClass.CONTEXT_FAILURE


class ResourceFailure(AixError):
    """Disk, memory or similar resource exhaustion."""

    failure_class = FailureClass.RESOURCE_FAILURE


class StoreError(ResourceFailure):
    """The event store is unusable (corrupt, too new, or holds an unknown record)."""


class BudgetExceeded(AixError):
    """A run budget was exceeded."""

    failure_class = FailureClass.BUDGET_EXCEEDED


class NoEligibleAgent(AixError):
    """No agent can take the task."""

    failure_class = FailureClass.NO_ELIGIBLE_AGENT


class Interrupted(AixError):
    """The orchestrator died or was killed mid-attempt."""

    failure_class = FailureClass.INTERRUPTED


class HumanRejection(AixError):
    """A human denied an approval."""

    failure_class = FailureClass.HUMAN_REJECTION


class IllegalTransition(AixError):
    """A state machine was asked to make a transition its table does not allow."""

    failure_class = FailureClass.TOOL_FAILURE


class ConfigError(AixError):
    """Configuration is invalid."""

    failure_class = FailureClass.POLICY_FAILURE


class UnsupportedError(AixError):
    """The agent or adapter does not support the requested operation."""

    failure_class = FailureClass.AGENT_FAILURE


def classify(exc: BaseException) -> FailureClass:
    """Map any exception to a ``FailureClass``.

    ``AixError`` uses its own class; well-known builtins are mapped generically; anything else is
    a ``TOOL_FAILURE``. Adapter-specific patterns are applied earlier by adapters (§19.1).
    """
    if isinstance(exc, AixError):
        return exc.failure_class
    if isinstance(exc, TimeoutError):
        return FailureClass.TIMEOUT
    if isinstance(exc, ConnectionError):
        return FailureClass.NETWORK_FAILURE
    if isinstance(exc, PermissionError):
        return FailureClass.POLICY_FAILURE
    if isinstance(exc, MemoryError):
        return FailureClass.RESOURCE_FAILURE
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EMFILE):
        return FailureClass.RESOURCE_FAILURE
    return FailureClass.TOOL_FAILURE


class IdenticalRetry(PolicyViolation):
    """A retry would repeat an earlier attempt exactly: same agent, prompt, base commit (§19.2)."""
