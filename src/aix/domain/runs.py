"""Run and Budget (§6, §9)."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import Field, model_validator

from aix.domain.base import DomainModel, UtcDatetime
from aix.domain.enums import RUN_TERMINAL, RunStatus
from aix.domain.ids import GraphId, RunId
from aix.domain.tasks import Intent


class Budget(DomainModel):
    """Per-run limits; defaults mirror the config defaults in §9."""

    max_cost_usd: float = Field(default=10.0, gt=0)
    max_attempts_total: int = Field(default=20, ge=1)
    max_wall_seconds: int = Field(default=7200, ge=1)


class Run(DomainModel):
    """One invocation of a user goal."""

    id: RunId
    project_root: Path
    goal: str = Field(min_length=1)
    intent: Intent | None = None
    graph_id: GraphId | None = None
    status: RunStatus = RunStatus.CREATED
    budget: Budget = Field(default_factory=Budget)
    created_at: UtcDatetime
    finished_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _finished_only_when_terminal(self) -> Self:
        if self.finished_at is not None and self.status not in RUN_TERMINAL:
            raise ValueError("finished_at may only be set for a terminal status")
        return self
