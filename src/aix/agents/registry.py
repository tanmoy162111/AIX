"""Adapter discovery, health and enable/disable (PLAYBOOK §10, §11, §25).

Adapters are plugins of type ``adapter``. Built-in ones are described by a manifest and loaded
through the same path as external ones (entry-point group ``aix.adapters``), found by *string*
(``importlib``), never by static import, so the core keeps no dependency on vendor code
(ADR-0003). An adapter package exposes ``create() -> AgentAdapter`` and ships ``manifest.yaml``
next to it. A broken built-in is a packaging bug and raises; a broken external plugin is skipped
and reported (:meth:`AdapterRegistry.plugin_records`, ``aix doctor``). Installing a plugin does
not enable it: agents still have to be listed in ``agents.enabled``.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from importlib import metadata, resources
from typing import Any

import yaml

from aix import __version__
from aix.agents.manifest import AdapterManifest
from aix.agents.protocol import AgentAdapter
from aix.config.schema import AixConfig
from aix.domain.agents import AgentSpec
from aix.domain.errors import ConfigError
from aix.plugins.loader import BUILTIN, PluginRecord, discover, load_object
from aix.plugins.manifest import PluginManifest, PluginType

BUILTIN_IDS: tuple[str, ...] = ("fake", "claude", "codex", "gemini", "opencode", "ollama")
"""Built-in adapter ids; extended as each adapter lands (M2.4 fake, M2.7 claude, ...)."""

SCRIPTED_ENV = "AIX_FAKE_SCRIPTS"
"""Setting this marks a demo/test session, in which the built-in ``fake`` agent is enabled."""


@dataclass(frozen=True)
class AdapterEntry:
    """A registered adapter and its manifest."""

    manifest: AdapterManifest
    adapter: AgentAdapter


def _read_manifest(package: str) -> AdapterManifest:
    """The ``manifest.yaml`` shipped in ``package``.

    Raises:
        ConfigError: the package or its manifest is missing or invalid.
    """
    try:
        text = resources.files(package).joinpath("manifest.yaml").read_text(encoding="utf-8")
        return AdapterManifest.model_validate(yaml.safe_load(text))
    except Exception as exc:
        raise ConfigError(f"adapter package {package!r}: cannot read manifest.yaml: {exc}") from exc


def builtin_plugin_manifest(adapter_id: str) -> PluginManifest:
    """The plugin manifest of a built-in adapter, derived from its ``manifest.yaml``.

    Raises:
        ConfigError: the adapter package is missing or its manifest is invalid.
    """
    package = f"aix.agents.adapters.{adapter_id}"
    try:
        adapter = _read_manifest(package)
    except ConfigError as exc:
        raise ConfigError(f"cannot load built-in adapter {adapter_id!r}: {exc}") from exc
    return PluginManifest(
        id=adapter_id,
        version=__version__,
        type=PluginType.ADAPTER,
        capabilities=sorted(c.value for c in adapter.capabilities),
        permissions=["spawn_process"] if adapter.kind == "cli" else [],
        requires_aix=">=0",
        entrypoint=f"{package}:create",
    )


def load_adapter(record: PluginRecord) -> AdapterEntry:
    """Import an adapter plugin and pair its ``create()`` factory with its ``manifest.yaml``.

    Raises:
        ConfigError: it cannot be imported, has no valid manifest, or the ids disagree.
    """
    assert record.manifest is not None
    create = load_object(record)
    module_name = record.manifest.entrypoint.partition(":")[0]
    module = importlib.import_module(module_name)
    package = module_name if hasattr(module, "__path__") else module_name.rpartition(".")[0]
    manifest = _read_manifest(package)
    if manifest.id != record.id:
        raise ConfigError(
            f"adapter package {package!r} declares id {manifest.id!r}, not {record.id!r}"
        )
    try:
        adapter: AgentAdapter = create()
    except Exception as exc:
        raise ConfigError(f"adapter {record.id!r}: create() failed: {exc}") from exc
    return AdapterEntry(manifest=manifest, adapter=adapter)


def load_builtin(adapter_id: str) -> AdapterEntry:
    """Import ``aix.agents.adapters.<id>`` and build its entry.

    Raises:
        ConfigError: the package is missing, has no manifest, or the ids disagree.
    """
    try:
        manifest = builtin_plugin_manifest(adapter_id)
        return load_adapter(PluginRecord(adapter_id, PluginType.ADAPTER, BUILTIN, manifest))
    except ConfigError as exc:
        raise ConfigError(f"cannot load built-in adapter {adapter_id!r}: {exc}") from exc


class AdapterRegistry:
    """Holds adapters, applies enablement from config and reports health."""

    def __init__(
        self,
        config: AixConfig,
        *,
        builtin_ids: tuple[str, ...] | None = None,
        discover_plugins: bool | None = None,
        plugin_entry_points: Callable[..., Iterable[Any]] = metadata.entry_points,
    ) -> None:
        """Register the built-in adapters and, unless disabled, external adapter plugins.

        ``discover_plugins`` defaults to on when ``builtin_ids`` is not given (the production
        registry) and off otherwise, so tests that pick their own agents stay hermetic.
        ``plugin_entry_points`` replaces ``importlib.metadata.entry_points`` (tests).
        """
        self._config = config
        self._entries: dict[str, AdapterEntry] = {}
        self._records: list[PluginRecord] = []
        ids = BUILTIN_IDS if builtin_ids is None else builtin_ids
        if discover_plugins is None:
            discover_plugins = builtin_ids is None
        builtins = [builtin_plugin_manifest(adapter_id) for adapter_id in ids]
        if discover_plugins:
            records = discover(
                PluginType.ADAPTER, builtins=builtins, entry_points_fn=plugin_entry_points
            )
        else:
            records = [PluginRecord(m.id, PluginType.ADAPTER, BUILTIN, m) for m in builtins]
        for record in records:
            if not record.ok:
                self._records.append(record)
                continue
            if record.source == BUILTIN:
                self.register(load_builtin(record.id))  # a broken built-in raises
                self._records.append(record)
                continue
            try:
                self.register(load_adapter(record))
            except ConfigError as exc:
                record = record.failed(str(exc))
            self._records.append(record)

    def plugin_records(self) -> list[PluginRecord]:
        """Every adapter plugin considered, built-in first; failed ones carry their ``error``."""
        return list(self._records)

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
        """Enabled by config (``agents.enabled``); ``fake-*`` test agents are always on.

        The built-in ``fake`` does nothing useful without a script, so it is on only when listed
        in ``agents.enabled`` or when ``AIX_FAKE_SCRIPTS`` is set; otherwise the router could hand
        it real tasks.
        """
        return (
            agent_id in self._config.agents.enabled
            or agent_id.startswith("fake-")
            or (agent_id == "fake" and bool(os.environ.get(SCRIPTED_ENV)))
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
