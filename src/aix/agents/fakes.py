"""Helpers to register extra fake agents (``fake-a``, ``fake-reviewer``) for tests and goldens."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import FakeScript
from aix.agents.manifest import AdapterManifest
from aix.agents.registry import AdapterEntry
from aix.domain.agents import AgentSupports
from aix.domain.enums import Capability


def make_fake_entry(
    agent_id: str,
    *,
    capabilities: dict[Capability, float] | None = None,
    scripts: list[FakeScript] | None = None,
    base_dir: Path | None = None,
    health: Literal["ready", "degraded", "unavailable", "disabled"] = "ready",
    health_reason: str | None = None,
    models: list[str] | None = None,
    default_model: str | None = None,
) -> AdapterEntry:
    """A registry entry for a scripted fake agent with its own id and capability priors."""
    adapter = FakeAdapter(
        agent_id,
        scripts=scripts,
        base_dir=base_dir,
        capabilities=capabilities,
        health=health,
        health_reason=health_reason,
        models=models,
        default_model=default_model,
    )
    manifest = AdapterManifest(
        id=agent_id,
        name=agent_id,
        kind="local",
        cost_class="free",
        capabilities=adapter._caps,  # pyright: ignore[reportPrivateUsage]
        supports=AgentSupports(streaming=True, cancel=True, cost_reporting="full"),
    )
    return AdapterEntry(manifest=manifest, adapter=adapter)
