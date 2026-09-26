"""Adapter discovery, health and enable/disable (PLAYBOOK §10, §11).

Built-in adapters are found by *string* (``importlib``), never by static import, so the core keeps
no dependency on vendor code (ADR-0003). Each adapter package under ``aix.agents.adapters.<id>``
exposes ``create() -> AgentAdapter`` and ships ``manifest.yaml``. Plugin entry points join in M9.
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources

import yaml

from aix.agents.manifest import AdapterManifest
from aix.agents.protocol import AgentAdapter
from aix.config.schema import AixConfig
from aix.domain.agents import AgentSpec
from aix.domain.errors import ConfigError

BUILTIN_IDS: tuple[str, ...] = ("fake", "claude", "codex")
"""Built-in adapter ids; extended as each adapter lands (M2.4 fake, M2.7 claude, ...)."""

ALWAYS_ENABLED: frozenset[str] = frozenset({"fake"})


@dataclass(frozen=True)
class AdapterEntry:
    """A registered adapter and its manifest."""

    manifest: AdapterManifest
    adapter: AgentAdapter


def load_builtin(adapter_id: str) -> AdapterEntry:
    """Import ``aix.agents.adapters.<id>`` and build its entry.

    Raises:
        ConfigError: the package is missing, has no manifest, or the ids disagree.
    """
    module_name = f"aix.agents.adapters.{adapter_id}"
    try:
        module = importlib.import_module(module_name)
        text = resources.files(module_name).joinpath("manifest.yaml").read_text(encoding="utf-8")
    except (ImportError, FileNotFoundError) as exc:
        raise ConfigError(f"cannot load built-in adapter {adapter_id!r}: {exc}") from exc
    manifest = AdapterManifest.model_validate(yaml.safe_load(text))
    if manifest.id != adapter_id:
        raise ConfigError(f"adapter package {adapter_id!r} declares id {manifest.id!r}")
    adapter: AgentAdapter = module.create()
    return AdapterEntry(manifest=manifest, adapter=adapter)


class AdapterRegistry:
    """Holds adapters, applies enablement from config and reports health."""

    def __init__(self, config: AixConfig, *, builtin_ids: tuple[str, ...] | None = None) -> None:
        self._config = config
        self._entries: dict[str, AdapterEntry] = {}
        for adapter_id in BUILTIN_IDS if builtin_ids is None else builtin_ids:
            self.register(load_builtin(adapter_id))

    def register(self, entry: AdapterEntry) -> None:
        """Add an adapter (tests register fakes with different ids/capabilities)."""
        if entry.manifest.id in self._entries:
            raise ConfigError(f"agent {entry.manifest.id!r} is already registered")
        self._entries[entry.manifest.id] = entry

    def ids(self) -> list[str]:
        """Registered ids in registration order."""
        return list(self._entries)

    def _entry(self, agent_id: str) -> AdapterEntry:
        try:
            return self._entries[agent_id]
        except KeyError:
            raise ConfigError(
                f"unknown agent {agent_id!r}; known: {', '.join(self._entries) or 'none'}"
            ) from None

    def get(self, agent_id: str) -> AgentAdapter:
        """The adapter for ``agent_id``."""
        return self._entry(agent_id).adapter

    def manifest(self, agent_id: str) -> AdapterManifest:
        """The manifest for ``agent_id``."""
        return self._entry(agent_id).manifest

    def is_enabled(self, agent_id: str) -> bool:
        """Enabled by config (``agents.enabled``); ``fake`` and ``fake-*`` are always on."""
        return (
            agent_id in ALWAYS_ENABLED
            or agent_id.startswith("fake-")
            or agent_id in self._config.agents.enabled
        )

    async def probe(self, agent_id: str) -> AgentSpec:
        """Probe one agent. Never raises for a broken adapter: that becomes ``unavailable``."""
        entry = self._entry(agent_id)
        try:
            spec = await entry.adapter.probe()
        except Exception as exc:  # adapters are third-party code
            return AgentSpec(
                id=agent_id,
                name=entry.manifest.name,
                kind=entry.manifest.kind,
                health="unavailable",
                health_reason=f"probe failed: {exc}",
            )
        if not self.is_enabled(agent_id):
            return spec.model_copy(
                update={"health": "disabled", "health_reason": "disabled in configuration"}
            )
        return spec

    async def probe_all(self) -> list[AgentSpec]:
        """Probe every registered agent, including disabled and unavailable ones."""
        return [await self.probe(agent_id) for agent_id in self._entries]

    async def eligible(self) -> list[AgentSpec]:
        """Enabled agents whose health is ``ready`` or ``degraded``."""
        return [s for s in await self.probe_all() if s.health in ("ready", "degraded")]


def overrides_for(config: AixConfig) -> Mapping[str, object]:
    """Per-agent overrides from config (model, timeout)."""
    return dict(config.agents.overrides)
