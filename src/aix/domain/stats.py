"""Observed agent statistics (PLAYBOOK §22)."""

from __future__ import annotations

from pydantic import Field

from aix.domain.base import DomainModel


class AgentStatsRow(DomainModel):
    """History of one (agent, model, task type). ``model`` is ``""`` when none was pinned."""

    agent_id: str
    model: str
    task_type: str
    attempts: int = Field(ge=0)
    accepted: int = Field(ge=0)
    verification_passed: int = Field(ge=0)
    retries: int = Field(ge=0)
    human_interventions: int = Field(ge=0)
    mean_cost_usd: float | None = None
    p50_latency_s: float | None = None
    p90_latency_s: float | None = None

    @property
    def accept_rate(self) -> float | None:
        return self.accepted / self.attempts if self.attempts else None

    @property
    def verification_pass_rate(self) -> float | None:
        """Share of attempts whose verification passed (process failures count against it)."""
        return self.verification_passed / self.attempts if self.attempts else None

    @property
    def retry_rate(self) -> float | None:
        return self.retries / self.attempts if self.attempts else None

    @property
    def human_rate(self) -> float | None:
        return self.human_interventions / self.attempts if self.attempts else None
