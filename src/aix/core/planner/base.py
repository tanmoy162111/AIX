"""Planner interface (PLAYBOOK §13.2)."""

from __future__ import annotations

from typing import Protocol

from aix.domain.tasks import Intent, RepoFacts, TaskGraph


class Planner(Protocol):
    """Turns an :class:`Intent` into a validated :class:`TaskGraph`."""

    async def plan(
        self, intent: Intent, repo_facts: RepoFacts, run_id: str, skill: str | None = None
    ) -> TaskGraph:
        """Return a graph whose tasks all belong to ``run_id``.

        ``skill`` forces a specific skill; ``None`` lets the planner choose.
        """
        ...
