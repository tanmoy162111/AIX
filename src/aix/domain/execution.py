"""Attempt, ExecutionResult and their parts (§6, §10.4)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from aix.domain.base import DomainModel, Sha256, UtcDatetime
from aix.domain.enums import AttemptStatus, FailureClass, RetryMutation
from aix.domain.ids import AttemptId, TaskId


class Attempt(DomainModel):
    """One try at a task by one agent."""

    id: AttemptId
    task_id: TaskId
    number: int = Field(ge=1)
    agent_id: str = Field(min_length=1)
    model: str | None = None
    mutation: RetryMutation | None = None
    """Why this attempt differs from the previous one (§19.2); ``None`` for the first attempt."""
    workspace: Path
    base_commit: str
    status: AttemptStatus = AttemptStatus.CREATED


class DiffSummary(DomainModel):
    """What changed in the workspace, computed by the control plane, never by the agent (§10.4)."""

    files_changed: int = Field(default=0, ge=0)
    lines_added: int = Field(default=0, ge=0)
    lines_removed: int = Field(default=0, ge=0)
    paths: list[str] = Field(default_factory=list)
    patch_sha256: Sha256 | None = None


class ToolCallRecord(DomainModel):
    """A tool call observed in an agent stream."""

    name: str
    ts: UtcDatetime | None = None
    ok: bool | None = None


class Usage(DomainModel):
    """Token and cost accounting. ``cost_usd`` is ``None`` if unknown; ``estimated`` if derived."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0)
    estimated: bool = False
    model: str | None = None


class ExecutionResult(DomainModel):
    """Process-level outcome of an attempt. ``claim`` is the agent's own words, NOT evidence."""

    attempt_id: AttemptId
    exit_code: int | None
    status: Literal["completed", "failed", "timeout", "cancelled"]
    failure: FailureClass | None = None
    claim: str | None = None
    diff: DiffSummary = Field(default_factory=DiffSummary)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list[ToolCallRecord])
    usage: Usage = Field(default_factory=Usage)
    duration_ms: int = Field(ge=0)
    stream_path: Path
