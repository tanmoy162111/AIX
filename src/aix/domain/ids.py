"""Prefixed ULID identifiers (PLAYBOOK §4).

Every entity id is ``<prefix>_<26-char Crockford ULID>``. The Pydantic-annotated aliases below
validate both the prefix and the ULID body, so a ``TaskId`` can never be passed where a ``RunId``
is expected at a model boundary.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import StringConstraints
from ulid import ULID

_ULID_BODY = r"[0-9A-HJKMNP-TV-Z]{26}"


class IdPrefix(StrEnum):
    """Known id prefixes."""

    RUN = "run"
    TASK = "task"
    GRAPH = "gph"
    ATTEMPT = "att"
    EXECUTION = "exe"
    CHECK = "chk"
    DECISION = "dec"
    APPROVAL = "apv"
    ARTIFACT = "art"
    EVENT = "evt"


def _pattern(prefix: IdPrefix) -> str:
    return rf"^{prefix.value}_{_ULID_BODY}$"


RunId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.RUN))]
TaskId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.TASK))]
GraphId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.GRAPH))]
AttemptId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.ATTEMPT))]
ExecutionId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.EXECUTION))]
CheckId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.CHECK))]
DecisionId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.DECISION))]
ApprovalId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.APPROVAL))]
ArtifactId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.ARTIFACT))]
EventId = Annotated[str, StringConstraints(pattern=_pattern(IdPrefix.EVENT))]


def new_id(prefix: IdPrefix) -> str:
    """Return a fresh, time-sortable id ``<prefix>_<ULID>``."""
    return f"{prefix.value}_{ULID()}"


def id_prefix(value: str) -> IdPrefix:
    """Return the prefix of ``value``.

    Raises:
        ValueError: if ``value`` is not a well-formed prefixed ULID with a known prefix.
    """
    head, sep, tail = value.partition("_")
    if not sep or len(tail) != 26:
        raise ValueError(f"malformed id: {value!r}")
    try:
        return IdPrefix(head)
    except ValueError:
        raise ValueError(f"unknown id prefix in {value!r}") from None
