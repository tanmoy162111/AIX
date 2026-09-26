"""Artifact and Provenance (§6, §21)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from aix.domain.base import DomainModel, Sha256, UtcDatetime
from aix.domain.enums import ArtifactType
from aix.domain.ids import ArtifactId, AttemptId, DecisionId, RunId, TaskId


class Producer(DomainModel):
    """Who or what produced an artifact."""

    kind: Literal["agent", "check", "control_plane", "reviewer"]
    id: str
    model: str | None = None
    version: str | None = None


class Provenance(DomainModel):
    """Who, what, from which inputs, under which policy (§21.2)."""

    run_id: RunId
    task_id: TaskId | None = None
    attempt_id: AttemptId | None = None
    producer: Producer
    tools: list[str] = Field(default_factory=list)
    inputs: list[ArtifactId] = Field(default_factory=list)
    base_commit: str | None = None
    result_commit: str | None = None
    decisions: list[DecisionId] = Field(default_factory=list)
    policy_hash: str | None = None
    aix_version: str
    created_at: UtcDatetime


class Artifact(DomainModel):
    """A durable, content-addressed output with provenance."""

    id: ArtifactId
    type: ArtifactType
    media_type: str
    sha256: Sha256
    size: int = Field(ge=0)
    path: Path
    """Path inside the object store."""
    provenance: Provenance
