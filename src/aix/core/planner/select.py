"""Planner selection from ``planner.provider`` (PLAYBOOK §13.2)."""

from __future__ import annotations

from dataclasses import dataclass, field

from aix.agents.registry import AdapterRegistry
from aix.config.schema import PlannerConfig
from aix.domain.enums import Capability

AUTO_MIN_DESIGN = 0.6
"""Minimum ``design`` prior for ``auto`` (§13.2)."""
NEVER_AUTO = frozenset({"fake"})
"""The built-in scripted agent is always enabled; it must not plan real runs by accident."""


@dataclass(frozen=True)
class PlannerChoice:
    """Which planner to use: ``agent_id is None`` means the deterministic template planner."""

    agent_id: str | None
    warnings: list[str] = field(default_factory=list[str])


async def choose_planner(config: PlannerConfig, registry: AdapterRegistry) -> PlannerChoice:
    """Resolve ``config.provider`` to a concrete choice.

    Contract: ``template`` -> template. ``agent:<id>`` -> that agent if ready or degraded, else
    the template with a warning. ``auto`` -> the ready (then degraded) enabled agent with the
    highest ``design`` prior >= ``AUTO_MIN_DESIGN`` (ties by id), never the built-in ``fake``;
    otherwise the template.

    Raises:
        ConfigError: ``agent:<id>`` names an agent that is not registered (from the registry).
    """
    provider = config.provider
    if provider == "template":
        return PlannerChoice(None)
    if provider.startswith("agent:"):
        agent_id = provider.removeprefix("agent:")
        spec = await registry.probe(agent_id)  # raises ConfigError for unknown ids
        if spec.health in ("ready", "degraded"):
            return PlannerChoice(agent_id)
        reason = spec.health_reason or spec.health
        return PlannerChoice(
            None, [f"planner agent {agent_id!r} is unavailable ({reason}); used template planner"]
        )
    candidates = [
        s
        for s in await registry.eligible()
        if s.id not in NEVER_AUTO and s.capabilities.get(Capability.DESIGN, 0.0) >= AUTO_MIN_DESIGN
    ]
    if not candidates:
        return PlannerChoice(None)
    best = min(
        candidates,
        key=lambda s: (s.health != "ready", -s.capabilities[Capability.DESIGN], s.id),
    )
    return PlannerChoice(best.id)
