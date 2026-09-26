"""Run event recording: one place that appends events and moves the run state machine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from aix.config.schema import AixConfig
from aix.domain.runs import Budget, Run
from aix.domain.state import RunEvent, transition_run
from aix.store import events as ev
from aix.store.db import EventStore


def new_run(run_id: str, root: Path, goal: str, config: AixConfig, now: datetime) -> Run:
    """A ``created`` run with the configured budget."""
    return Run(
        id=run_id,
        project_root=root,
        goal=goal,
        budget=Budget(
            max_cost_usd=config.budget.max_cost_usd_per_run,
            max_attempts_total=config.budget.max_attempts_per_run,
            max_wall_seconds=config.budget.max_wall_seconds_per_run,
        ),
        created_at=now,
    )


class RunRecorder:
    """Appends events for one run and applies run-level transitions through the state machine."""

    def __init__(self, store: EventStore, run: Run, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._run = run
        self._clock = clock

    @property
    def run(self) -> Run:
        """The current run state."""
        return self._run

    def now(self) -> datetime:
        """The injected clock."""
        return self._clock()

    async def emit(
        self,
        etype: str,
        payload: ev.Payload,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> None:
        """Append one event for this run."""
        await self._store.append(
            etype,
            payload,
            run_id=self._run.id,
            task_id=task_id,
            attempt_id=attempt_id,
            ts=self._clock(),
        )

    async def run_to(self, event: RunEvent) -> None:
        """Apply a run transition and record ``run.state_changed``.

        Raises:
            IllegalTransition: the run state machine has no such edge.
        """
        new = transition_run(self._run, event, at=self._clock())
        await self.emit(
            "run.state_changed",
            ev.RunStateChangedPayload(
                from_status=self._run.status, to_status=new.status, event=event
            ),
        )
        self._run = new

    async def start(self) -> None:
        """Record ``run.created`` and enter ``planning``."""
        await self.emit("run.created", ev.RunCreatedPayload(run=self._run))
        await self.run_to(RunEvent.START_PLANNING)
