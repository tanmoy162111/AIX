"""Run budget tracking (PLAYBOOK §14.1): cost, attempts and wall time."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from aix.domain.runs import Budget

BudgetKind = Literal["cost_usd", "attempts", "wall_seconds"]


@dataclass(frozen=True)
class Overrun:
    """A budget that is exceeded (or would be by starting another attempt)."""

    budget: BudgetKind
    limit: float
    actual: float


class BudgetTracker:
    """Tracks what a run has used against its :class:`Budget`.

    Contract: cost is compared strictly (spending exactly the limit is allowed); the attempt
    limit is checked *before* starting an attempt; wall time runs from construction. Unknown
    (``None``) costs are ignored; estimation arrives with M7.5.
    """

    def __init__(self, budget: Budget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._budget = budget
        self._clock = clock
        self._start = clock()
        self.attempts = 0
        self.cost_usd = 0.0

    def start_attempt(self) -> None:
        """Record that an attempt began."""
        self.attempts += 1

    def add_cost(self, usd: float | None) -> None:
        """Add an attempt's reported cost."""
        if usd is not None:
            self.cost_usd += usd

    def elapsed_s(self) -> float:
        return self._clock() - self._start

    def exceeded(self) -> Overrun | None:
        """The first budget that has been overrun (cost, then wall time), else ``None``."""
        if self.cost_usd > self._budget.max_cost_usd:
            return Overrun("cost_usd", self._budget.max_cost_usd, self.cost_usd)
        if self.elapsed_s() > self._budget.max_wall_seconds:
            return Overrun("wall_seconds", float(self._budget.max_wall_seconds), self.elapsed_s())
        return None

    def can_start_attempt(self) -> Overrun | None:
        """``None`` if another attempt may start, else the budget that forbids it."""
        if (over := self.exceeded()) is not None:
            return over
        if self.attempts >= self._budget.max_attempts_total:
            return Overrun("attempts", float(self._budget.max_attempts_total), float(self.attempts))
        return None

    def used_ratio(self, kind: BudgetKind) -> float:
        """Fraction of the ``kind`` budget used (1.0 = at the limit)."""
        if kind == "cost_usd":
            return self.cost_usd / self._budget.max_cost_usd
        if kind == "attempts":
            return self.attempts / self._budget.max_attempts_total
        return self.elapsed_s() / self._budget.max_wall_seconds
