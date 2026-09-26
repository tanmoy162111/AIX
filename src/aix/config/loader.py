"""Layered config loader with per-key source tracking (PLAYBOOK §9).

Layers, later overriding earlier: built-in defaults -> user file -> project file -> skill defaults
-> task overrides -> CLI flags. Mappings merge recursively; lists and scalars replace. Every leaf of
the result records which layer set it, so ``aix config show --resolved`` can explain itself.
"""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from aix.config.schema import AixConfig
from aix.domain.errors import ConfigError

DEFAULT_SOURCE = "default"
_SECRET_KEY = re.compile(r"(^|_)(api_?key|secret|password|token|credentials?)$", re.IGNORECASE)


def user_config_path(env: Mapping[str, str] | None = None) -> Path:
    """``$XDG_CONFIG_HOME/aix/config.yaml`` or ``~/.config/aix/config.yaml``."""
    env = os.environ if env is None else env
    xdg = env.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "aix" / "config.yaml"
    home = env.get("HOME")
    return (Path(home) if home else Path.home()) / ".config" / "aix" / "config.yaml"


def project_config_path(project_root: Path) -> Path:
    """``<project>/.aix/config.yaml``."""
    return project_root / ".aix" / "config.yaml"


def _leaves(node: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten nested mappings to ``(dotted.path, value)``; empty mappings and lists are leaves."""
    if isinstance(node, Mapping) and node:
        out: list[tuple[str, Any]] = []
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            out.extend(_leaves(value, f"{prefix}.{key}" if prefix else str(key)))  # pyright: ignore[reportUnknownArgumentType]
        return out
    return [(prefix, node)]


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> None:
    for key, value in overlay.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)  # pyright: ignore[reportUnknownArgumentType]
        else:
            base[key] = copy.deepcopy(value)


def _reject_secrets(node: Any, origin: str, prefix: str = "") -> None:
    if isinstance(node, Mapping):
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            path = f"{prefix}.{key}" if prefix else str(key)
            if _SECRET_KEY.search(str(key)):
                raise ConfigError(
                    f"{origin}: secret-looking key {path!r}; secrets come from the environment or "
                    "OS keychain, never from config files"
                )
            _reject_secrets(value, origin, path)


def _read_file(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping, got {type(doc).__name__}")  # pyright: ignore[reportUnknownArgumentType]
    _reject_secrets(doc, str(path))
    return doc  # pyright: ignore[reportUnknownVariableType]


@dataclass(frozen=True)
class ResolvedConfig:
    """The merged config plus provenance."""

    config: AixConfig
    sources: dict[str, str]
    files: list[Path] = field(default_factory=list[Path])
    """Config files that existed and were merged, in precedence order."""

    def rows(self) -> list[tuple[str, Any, str]]:
        """``(dotted key, value, source)`` for every leaf, in schema order."""
        dumped = self.config.model_dump(mode="json")
        return [(k, v, self.sources.get(k, DEFAULT_SOURCE)) for k, v in _leaves(dumped)]


def load_config(
    project_root: Path,
    *,
    user_config: Path | None = None,
    skill_defaults: Mapping[str, Any] | None = None,
    task_overrides: Mapping[str, Any] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> ResolvedConfig:
    """Merge all layers and validate.

    ``skill_defaults``, ``task_overrides`` and ``cli_overrides`` are nested mappings (not dotted
    keys, because threshold names themselves contain dots).

    Raises:
        ConfigError: unreadable/invalid YAML, a secret-looking key in a file, an unknown key or an
            invalid value; the message names the key and the layer that set it.
    """
    merged: dict[str, Any] = AixConfig().model_dump(mode="json")
    sources: dict[str, str] = {}
    files: list[Path] = []

    user_path = user_config if user_config is not None else user_config_path()
    proj_path = project_config_path(project_root)
    layers: list[tuple[str, Mapping[str, Any] | None]] = []
    for label, path in (("user", user_path), ("project", proj_path)):
        doc = _read_file(path)
        if doc is not None:
            files.append(path)
        layers.append((f"{label}:{path}", doc))
    layers.append(("skill", skill_defaults))
    layers.append(("task", task_overrides))
    layers.append(("cli", cli_overrides))

    for label, overlay in layers:
        if not overlay:
            continue
        _merge(merged, overlay)
        for path_, _ in _leaves(overlay):
            sources[path_] = label

    try:
        config = AixConfig.model_validate(merged)
    except ValidationError as exc:
        lines: list[str] = []
        for err in exc.errors():
            key = ".".join(str(p) for p in err["loc"])
            origin = sources.get(key, DEFAULT_SOURCE)
            lines.append(f"{key}: {err['msg']} (set by {origin})")
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(lines)) from exc

    final = {k: sources.get(k, DEFAULT_SOURCE) for k, _ in _leaves(config.model_dump(mode="json"))}
    return ResolvedConfig(config=config, sources=final, files=files)
