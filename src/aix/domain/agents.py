"""AgentSpec: what an adapter reports about its agent (§6, §10.2)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator

from aix.domain.base import DomainModel
from aix.domain.enums import Capability


class AgentSupports(DomainModel):
    """Feature flags an adapter declares; the policy engine uses them for eligibility (§20.2)."""

    streaming: bool = False
    sessions: bool = False
    non_interactive: bool = True
    cancel: bool = True
    cost_reporting: Literal["none", "partial", "full"] = "none"
    model_select: bool = False


class AgentSpec(DomainModel):
    """Manifest plus probe result for one agent."""

    id: str = Field(min_length=1)
    name: str
    kind: Literal["cli", "api", "local"]
    version: str | None = None
    capabilities: dict[Capability, float] = Field(default_factory=dict[Capability, float])
    """Self-declared prior strength per capability, in 0..1."""
    supports: AgentSupports = Field(default_factory=AgentSupports)
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    cost_class: Literal["free", "low", "medium", "high"] = "medium"
    health: Literal["ready", "degraded", "unavailable", "disabled"] = "unavailable"
    health_reason: str | None = None
    """Why health is not ``ready`` (§11)."""

    @field_validator("capabilities")
    @classmethod
    def _priors_in_unit_interval(cls, value: dict[Capability, float]) -> dict[Capability, float]:
        for cap, prior in value.items():
            if not 0.0 <= prior <= 1.0:
                raise ValueError(
                    f"capability prior for {cap.value} must be within 0..1, got {prior}"
                )
        return value
