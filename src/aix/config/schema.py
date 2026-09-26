"""Typed configuration schema; defaults mirror PLAYBOOK §9.

Unknown keys are rejected (``extra="forbid"``) so typos surface as errors instead of being ignored.
Secrets never live here (§9, §20.5).
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from aix.domain.base import DomainModel
from aix.domain.enums import CheckKind


class AgentOverride(DomainModel):
    """Per-agent overrides."""

    model: str | None = None
    timeout_s: int | None = Field(default=None, ge=1)


def _default_agent_overrides() -> dict[str, AgentOverride]:
    return {"codex": AgentOverride(model=None, timeout_s=1800)}


class AgentsConfig(DomainModel):
    enabled: list[str] = Field(default_factory=lambda: ["claude", "codex", "gemini", "opencode"])
    """``fake`` is always available in tests and need not be listed."""
    overrides: dict[str, AgentOverride] = Field(default_factory=_default_agent_overrides)


class RoutingConfig(DomainModel):
    strategy: Literal["capability", "static", "jev_assisted"] = "capability"
    static: dict[str, str] = Field(default_factory=dict[str, str])
    """Optional pins, e.g. ``{implement: codex}``."""
    prefer_independent_reviewer: bool = True


class PlannerConfig(DomainModel):
    provider: str = Field(default="auto", pattern=r"^(auto|template|agent:[A-Za-z0-9_.-]+)$")
    max_tasks: int = Field(default=12, ge=1)


class ExecutionConfig(DomainModel):
    max_parallel: int = Field(default=3, ge=1)
    attempt_timeout_s: int = Field(default=1800, ge=1)
    workspace: Literal["worktree", "inplace"] = "worktree"


class SecurityChecksConfig(DomainModel):
    sast: Literal["auto", "on", "off"] = "auto"
    secrets: Literal["auto", "on", "off"] = "auto"
    deps: Literal["auto", "on", "off"] = "auto"


class VerificationConfig(DomainModel):
    required_default: list[CheckKind] = Field(
        default_factory=lambda: [CheckKind.BUILD, CheckKind.TESTS, CheckKind.LINT]
    )
    commands: dict[CheckKind, list[str]] = Field(default_factory=dict[CheckKind, list[str]])
    """Explicit command overrides, e.g. ``{tests: [pytest, -q]}``; they always win (§17.1)."""
    security: SecurityChecksConfig = Field(default_factory=SecurityChecksConfig)


class JevConfig(DomainModel):
    model: str | None = None
    timeout_ms: int = Field(default=3000, ge=1)
    on_error: Literal["fallback_rules"] = "fallback_rules"


def _default_thresholds() -> dict[str, float]:
    return {
        "task_completion.accept_confidence": 0.85,
        "task_completion.accept_confidence_high": 0.92,
        "task_completion.low_confidence": 0.5,
        "task_completion.max_blocking_noul": 0.3,
        "task_completion.max_residual_risk": 1.5,
        "failure_triage.min_confidence": 0.6,
        "tool_risk.deny_if_p_risky_above": 0.3,
        "tool_risk.deny_risk_score": 2.5,
        "tool_risk.ask_risk_score": 1.5,
        "plan_review.side_effect_noul": 0.5,
        "plan_review.missing_verification_noul": 0.5,
        "plan_review.ask_risk_score": 1.5,
        "routing.min_confidence": 0.5,
    }


class DecisionConfig(DomainModel):
    provider: Literal["rules", "jev", "fake"] = "rules"
    jev: JevConfig = Field(default_factory=JevConfig)
    thresholds: dict[str, float] = Field(default_factory=_default_thresholds)
    """Per decision point, keyed ``<point>.<name>`` (§18.5)."""


class BudgetConfig(DomainModel):
    max_cost_usd_per_run: float = Field(default=10.0, gt=0)
    max_attempts_per_run: int = Field(default=20, ge=1)
    max_wall_seconds_per_run: int = Field(default=7200, ge=1)


class NetworkConfig(DomainModel):
    agents: str = "provider_default"
    checks: Literal["deny", "allow"] = "deny"


class SecurityConfig(DomainModel):
    sandbox: Literal["local", "container"] = "local"
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    shell_allow: list[str] = Field(
        default_factory=lambda: [
            "git",
            "python",
            "pytest",
            "ruff",
            "mypy",
            "npm",
            "node",
            "pnpm",
            "yarn",
            "go",
            "cargo",
            "make",
            "uv",
        ]
    )
    approval_required_for: list[str] = Field(
        default_factory=lambda: [
            "deploy",
            "db_migration_apply",
            "secrets_write",
            "push",
            "delete_outside_scope",
        ]
    )


class ArtifactsConfig(DomainModel):
    bundle: bool = True
    formats: list[Literal["json", "md", "html"]] = Field(
        default_factory=lambda: ["json", "md", "html"]
    )


class AixConfig(DomainModel):
    """The fully merged configuration."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    decision: DecisionConfig = Field(default_factory=DecisionConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)
