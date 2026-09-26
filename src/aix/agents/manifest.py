"""Adapter manifest schema (PLAYBOOK §10.2). Each adapter ships a ``manifest.yaml``."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator

from aix.domain.agents import AgentSupports
from aix.domain.base import DomainModel
from aix.domain.enums import Capability

EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Z_][A-Z0-9_]*$")]


class ProbeSpec(DomainModel):
    """How to interrogate the binary without spending money."""

    version_args: list[str] = Field(default_factory=lambda: ["--version"])
    help_args: list[str] = Field(default_factory=lambda: ["--help"])
    auth_check: str | None = None
    """e.g. ``exec_smoke``: a real (paid) call, only run by ``aix agent test`` / live tests."""


class AdapterManifest(DomainModel):
    """Static description of an adapter and its agent."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    name: str
    kind: Literal["cli", "api", "local"]
    binary: str | None = None
    probe: ProbeSpec = Field(default_factory=ProbeSpec)
    required_flags: list[str] = Field(default_factory=list)
    """Flags that must appear in the CLI's help text; otherwise health is ``degraded`` (§11)."""
    capabilities: dict[Capability, float] = Field(default_factory=dict[Capability, float])
    cost_class: Literal["free", "low", "medium", "high"] = "medium"
    supports: AgentSupports = Field(default_factory=AgentSupports)
    models: list[str] = Field(default_factory=list)
    default_model: str | None = None
    env_allowlist: list[EnvName] = Field(default_factory=list)
    """Env var names this adapter's process may receive besides the minimal base (§20.5)."""

    @field_validator("capabilities")
    @classmethod
    def _priors_in_unit_interval(cls, value: dict[Capability, float]) -> dict[Capability, float]:
        for cap, prior in value.items():
            if not 0.0 <= prior <= 1.0:
                raise ValueError(f"capability prior for {cap.value} must be within 0..1")
        return value
