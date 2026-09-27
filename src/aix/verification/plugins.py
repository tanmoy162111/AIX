"""Check plugins (PLAYBOOK §17.3, §25): third-party checks that run beside the built-in ones.

A plugin of type ``check`` has an entrypoint that is an ``async def check(ctx: CheckContext) ->
PluginCheckResult``. The engine turns its result into a ``custom`` :class:`Check`; the plugin
never sets ``required`` or severity itself, the project's configuration does.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from aix.domain.verification import CheckStatus


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
