"""Check plugins (PLAYBOOK §17.3, §25): third-party checks that run beside the built-in ones.

A plugin of type ``check`` has an entrypoint that is an ``async def check(ctx: CheckContext) ->
PluginCheckResult``. The engine turns its result into a ``custom`` :class:`Check`; the plugin
never sets ``required`` or severity itself, the project's configuration does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

from aix.config.schema import AixConfig
from aix.domain.verification import CheckStatus
from aix.plugins.loader import discover, load_object
from aix.plugins.manifest import PluginType


@dataclass(frozen=True)
class CheckContext:
    """What a check plugin may look at: the attempt's worktree and the change under test."""

    workspace: Path
    patch: str
    changed_paths: Sequence[str]
    file_scope: Sequence[str]
    goal: str = ""


@dataclass(frozen=True)
class PluginCheckResult:
    """A plugin's verdict, factual and short: ``status`` is passed, failed, warning or skipped."""

    status: CheckStatus
    summary: str
    metrics: dict[str, float] = field(default_factory=dict[str, float])


PluginCheck = Callable[[CheckContext], Awaitable[PluginCheckResult]]


@dataclass(frozen=True)
class ActivePluginCheck:
    """A configured check plugin: runnable (``run``) or unavailable (``error``)."""

    id: str
    required: bool
    run: PluginCheck | None = None
    error: str | None = None


def load_plugin_checks(
    config: AixConfig,
    *,
    entry_points_fn: Callable[..., Iterable[Any]] = metadata.entry_points,
) -> list[ActivePluginCheck]:
    """Activate the check plugins named in ``verification.plugin_checks``.

    Never raises: a plugin that is not installed, is incompatible or fails to import comes back
    with its ``error`` so the engine records an ``error`` check (a required one blocks the task;
    verification is never silently skipped).
    """
    refs = config.verification.plugin_checks
    if not refs:
        return []
    records = {r.id: r for r in discover(PluginType.CHECK, entry_points_fn=entry_points_fn)}
    active: list[ActivePluginCheck] = []
    for ref in refs:
        record = records.get(ref.id)
        if record is None:
            active.append(ActivePluginCheck(ref.id, ref.required, error="plugin is not installed"))
            continue
        try:
            fn = load_object(record)
        except Exception as exc:
            active.append(ActivePluginCheck(ref.id, ref.required, error=str(exc)))
            continue
        active.append(ActivePluginCheck(ref.id, ref.required, run=fn))
    return active
