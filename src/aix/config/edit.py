"""Programmatic edits of the project config file (used by `aix agent enable|disable`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from aix.config.loader import load_config, project_config_path
from aix.domain.errors import ConfigError


def set_agent_enabled(
    project_root: Path, agent_id: str, enabled: bool, *, user_config: Path | None = None
) -> list[str]:
    """Persist ``agent_id`` in / out of ``agents.enabled`` in ``.aix/config.yaml``.

    The starting list is the *resolved* one, so the first edit materializes the defaults. Comments
    are kept when the file has no active keys yet (e.g. the file `aix init` writes); once it has
    active YAML it is re-serialized. Returns the new list.

    Raises:
        ConfigError: the existing file is not a YAML mapping.
    """
    current = list(load_config(project_root, user_config=user_config).config.agents.enabled)
    updated = [a for a in current if a != agent_id]
    if enabled:
        updated = [*current, agent_id] if agent_id not in current else current

    path = project_config_path(project_root)
    raw = path.read_text(encoding="utf-8") if path.exists() else ""
    doc: Any = yaml.safe_load(raw) if raw.strip() else None
    if doc is not None and not isinstance(doc, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    had_active_keys = bool(doc)
    data: dict[str, Any] = dict(doc) if doc else {}  # pyright: ignore[reportUnknownArgumentType]
    agents = dict(data.get("agents") or {})
    agents["enabled"] = updated
    data["agents"] = agents

    dumped = yaml.safe_dump(data, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if had_active_keys or not raw.strip():
        path.write_text(dumped, encoding="utf-8")
    else:
        path.write_text(raw.rstrip("\n") + "\n\n" + dumped, encoding="utf-8")
    return updated
