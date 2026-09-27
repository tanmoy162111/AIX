"""Plugin discovery through entry points (PLAYBOOK §25).

Each plugin type has an entry-point group (``aix.adapters``, ``aix.tools``, ``aix.skills``,
``aix.checks``, ``aix.decision_providers``, ``aix.exporters``). An entry point named after the
plugin id resolves to its *manifest* (a :class:`PluginManifest`, a mapping, or a zero-argument
callable returning either). The manifest is validated (fields, type, id, aix version range)
first; the plugin's real code, named by ``manifest.entrypoint``, is imported only on activation
(:func:`load_object`). A plugin that fails any step becomes a failed :class:`PluginRecord`; it is
skipped and reported (``aix doctor``), never raised. Built-in components pass their own manifests
through the same path.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from importlib import metadata
from typing import Any, Final

from pydantic import ValidationError

from aix import __version__
from aix.domain.errors import ConfigError
from aix.plugins.manifest import PluginManifest, PluginType

GROUPS: Final[dict[PluginType, str]] = {
    PluginType.ADAPTER: "aix.adapters",
    PluginType.TOOL: "aix.tools",
    PluginType.SKILL: "aix.skills",
    PluginType.CHECK: "aix.checks",
    PluginType.DECISION_PROVIDER: "aix.decision_providers",
    PluginType.EXPORTER: "aix.exporters",
}
BUILTIN: Final = "builtin"


@dataclass(frozen=True)
class PluginRecord:
    """A discovered plugin: usable when ``error`` is ``None``."""

    id: str
    type: PluginType
    source: str
    """``builtin`` or ``<distribution> (<entry point value>)``."""
    manifest: PluginManifest | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def failed(self, error: str) -> PluginRecord:
        """This record marked unusable (e.g. its code failed to import)."""
        return replace(self, error=error)


def _as_manifest(raw: object) -> PluginManifest:
    """Turn what an entry point resolved to into a manifest.

    Raises:
        ValueError: it is none of the accepted shapes.
        ValidationError: the manifest is invalid.
    """
    if callable(raw) and not isinstance(raw, PluginManifest):
        raw = raw()
    if isinstance(raw, PluginManifest):
        return raw
    if isinstance(raw, Mapping):
        return PluginManifest.model_validate(dict(raw))  # pyright: ignore[reportUnknownArgumentType]
    raise ValueError(f"expected a manifest, a mapping or a callable, got {type(raw).__name__}")


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'manifest'}: {e['msg']}" for e in exc.errors()
    )


def discover(
    type_: PluginType,
    *,
    builtins: Iterable[PluginManifest] = (),
    aix_version: str = __version__,
    entry_points_fn: Callable[..., Iterable[Any]] = metadata.entry_points,
) -> list[PluginRecord]:
    """Built-in manifests followed by every valid or invalid entry point of ``type_``.

    Entry points are sorted by name for stable output. The first plugin with an id wins; a later
    one is reported as a duplicate.
    """
    records: list[PluginRecord] = []
    seen: set[str] = set()

    def add(record: PluginRecord) -> None:
        if record.id in seen:
            record = record.failed(f"duplicate plugin id {record.id!r}")
        seen.add(record.id)
        records.append(record)

    for manifest in builtins:
        add(PluginRecord(manifest.id, type_, BUILTIN, manifest))

    for ep in sorted(entry_points_fn(group=GROUPS[type_]), key=lambda e: e.name):
        dist = getattr(getattr(ep, "dist", None), "name", None) or "unknown"
        source = f"{dist} ({ep.value})"
        manifest: PluginManifest | None = None
        error: str | None = None
        try:
            manifest = _as_manifest(ep.load())
        except ValidationError as exc:
            error = f"invalid manifest: {_summarize(exc)}"
        except Exception as exc:  # plugin code runs at import: anything can happen
            error = f"cannot load entry point: {type(exc).__name__}: {exc}"
        if manifest is not None:
            error = _check(manifest, ep.name, type_, aix_version)
        add(PluginRecord(ep.name, type_, source, manifest, error))
    return records


def _check(manifest: PluginManifest, name: str, type_: PluginType, aix_version: str) -> str | None:
    if manifest.id != name:
        return f"manifest id {manifest.id!r} does not match its entry point name {name!r}"
    if manifest.type is not type_:
        return f"manifest type is {manifest.type.value!r} but it is registered as {type_.value!r}"
    if not manifest.supports(aix_version):
        return f"requires aix {manifest.requires_aix}, this is aix {aix_version}"
    return None


def load_object(record: PluginRecord) -> Any:
    """Import and return the object named by the plugin's ``entrypoint`` (``module:attr``).

    Raises:
        ConfigError: the record failed validation, or the module/attribute cannot be imported.
    """
    if record.error is not None or record.manifest is None:
        raise ConfigError(f"plugin {record.id!r} is unusable: {record.error}")
    module_name, _, attr = record.manifest.entrypoint.partition(":")
    try:
        obj: Any = importlib.import_module(module_name)
        for part in attr.split("."):
            obj = getattr(obj, part)
    except Exception as exc:
        raise ConfigError(
            f"plugin {record.id!r}: cannot import {record.manifest.entrypoint!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return obj
