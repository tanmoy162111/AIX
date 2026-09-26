"""Per-task retry, escalation and mutation bookkeeping (PLAYBOOK §19.2-§19.3).

Turns a Decision Service outcome into a concrete :class:`NextAction` and keeps the state that
makes retries differ: which mutations a failure class has used, how far up the escalation ladder
the task is, the attempts it has consumed, and its failure history. Pure and synchronous.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from aix.core.escalation import Escalation, EscalationState, Step, next_escalation
from aix.core.failure import Classified
from aix.core.retry import Step as RetryStep
from aix.core.retry import next_step
from aix.domain.agents import AgentSpec
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import RetryMutation as M
from aix.domain.state import TaskEvent

_FAILURE_CONTEXT = (
    M.SAME_AGENT_WITH_FAILURE_CONTEXT,
    M.SAME_AGENT_WITH_FINDINGS,
    M.ADD_RESEARCH_STEP,
)
MAX_NOTE_CHARS = 4000
"""Cap on failure context fed back to an agent (§19.2: at most ~4k tokens)."""


@dataclass(frozen=True)
class NextAction:
    """What the executor does after a decision."""

    kind: Literal["accept", "fail", "ask_human", "retry"]
    event: TaskEvent
    mutation: M | None = None
    notes: tuple[str, ...] = ()
    exclude_agent: str | None = None
    force_agent: str | None = None
    model: str | None = None
    require_review: bool = False
    wait_s: float = 0.0
    consumes_attempt: bool = True
    mark_unavailable: str | None = None
    split: bool = False
    reasons: tuple[str, ...] = ()


def _note(mutation: M, attempt_no: int, detail: str, scope: Sequence[str]) -> tuple[str, ...]:
    """Control-plane facts for the next prompt. Never agent prose; always unique per retry."""
    head = f"Retry {attempt_no} ({mutation.value})."
    ctx = detail[:MAX_NOTE_CHARS]
    if mutation in (
        M.SAME_AGENT_WITH_FAILURE_CONTEXT,
        M.SAME_AGENT_WITH_FINDINGS,
        M.ADD_RESEARCH_STEP,
    ):
        return (head, f"The previous attempt failed verification: {ctx}")
    if mutation is M.SAME_AGENT_CLARIFIED_PROMPT:
        return (head, "The previous attempt changed no files. This task requires file changes "
                f"within: {', '.join(scope) or 'the workspace'}.")  # fmt: skip
    if mutation is M.SAME_AGENT_WITH_SCOPE_REMINDER:
        return (head, f"The previous attempt changed files outside its scope. Only change: "
                f"{', '.join(scope) or 'the task scope'}.")  # fmt: skip
    if mutation is M.SWITCH_AGENT:
        return (head, f"A different agent takes over after: {ctx}")
    if mutation is M.WAIT_AND_RETRY:
        return (head, "The previous attempt failed for a transient reason; try again.")
    if mutation is M.COMPACT_CONTEXT_AND_RETRY:
        return (head, "The previous attempt ran out of context; keep your working set small.")
    return (head, f"Start again with a fresh approach. Last failure: {ctx}")


@dataclass
class TaskPolicy:
    """Mutable per-task retry state."""

    max_attempts: int
    ladder: Sequence[Step]
    scope: Sequence[str] = ()
    consumed: int = 0
    used: dict[str, list[M]] = field(default_factory=dict[str, list[M]])
    escalation: EscalationState = field(default_factory=EscalationState)
    previous_failures: list[Classified] = field(default_factory=list[Classified])
    agent_switched: bool = False
    failed_agents: set[str] = field(default_factory=set[str])
    retries: int = 0

    @property
    def escalation_remaining(self) -> bool:
        return self.escalation.remaining(self.ladder)

    def begin_attempt(self) -> None:
        """An attempt is starting: it counts toward ``max_attempts`` until refunded."""
        self.consumed += 1

    def _ask(self, reasons: tuple[str, ...]) -> NextAction:
        return NextAction("ask_human", TaskEvent.ASK_HUMAN, reasons=reasons)

    def _escalate(
        self, current: AgentSpec, model: str | None, candidates: Sequence[AgentSpec]
    ) -> NextAction:
        esc: Escalation | None = next_escalation(
            self.ladder,
            self.escalation,
            current=current,
            current_model=model,
            candidates=candidates,
        )
        if esc is None or esc.step is Step.HUMAN:
            return self._ask(("escalation:human",))
        self.retries += 1
        notes = (f"Retry {self.retries} (escalation:{esc.step.value}). Use extra care.",)
        if esc.require_review:
            notes += ("An independent reviewer will examine your change and its findings will "
                      "be given to you if it is not acceptable.",)  # fmt: skip
        if esc.step is Step.DIFFERENT_AGENT:
            self.agent_switched = True
        return NextAction(
            "retry",
            TaskEvent.ESCALATE,
            notes=notes,
            force_agent=esc.agent_id,
            model=esc.model,
            require_review=esc.require_review,
            reasons=(f"escalation:{esc.step.value}",),
        )

    def plan_next(
        self,
        outcome: O,
        cls: Classified | None,
        *,
        current: AgentSpec,
        current_model: str | None,
        candidates: Sequence[AgentSpec],
        detail: str = "",
    ) -> NextAction:
        """Map a decision ``outcome`` for the just-finished attempt to the next action.

        Contract: ``accept``/``reject``/``stop``/``ask_human`` map directly. ``escalate`` climbs
        the ladder (human step or exhausted ladder asks a human). ``retry``/``switch_agent`` take
        the next mutation of the failure class's §19.2 sequence (``switch_agent`` forces a
        switch); an exhausted sequence escalates. Non-consuming steps refund the attempt.
        """
        if outcome is O.ACCEPT:
            return NextAction("accept", TaskEvent.ACCEPT)
        if outcome is O.REJECT:
            return NextAction("fail", TaskEvent.REJECT, reasons=("decision:reject",))
        if outcome is O.STOP:
            return NextAction("fail", TaskEvent.STOP, reasons=("decision:stop",))
        if outcome is O.ASK_HUMAN:
            return self._ask(("decision:ask_human",))
        if outcome is O.ESCALATE:
            return self._escalate(current, current_model, candidates)
        if cls is None:
            return self._ask(("no_classification",))
        self.previous_failures.append(cls)
        used = self.used.setdefault(cls.label, [])
        step = next_step(cls, used)
        if outcome is O.SWITCH_AGENT and (step is None or step.mutation is not M.SWITCH_AGENT):
            step = RetryStep(M.SWITCH_AGENT)
        if step is None:
            return self._escalate(current, current_model, candidates)
        used.append(step.mutation)
        if step.mutation is M.ASK_HUMAN:
            return self._ask(("mutation:ask_human",))
        if not step.consumes_attempt:
            self.consumed = max(0, self.consumed - 1)
        self.retries += 1
        switching = step.mutation is M.SWITCH_AGENT
        if switching:
            self.agent_switched = True
        return NextAction(
            "retry",
            TaskEvent.SWITCH_AGENT if switching else TaskEvent.RETRY,
            mutation=step.mutation,
            notes=_note(step.mutation, self.retries, detail, self.scope),
            exclude_agent=current.id if switching else None,
            wait_s=step.wait_s,
            consumes_attempt=step.consumes_attempt,
            mark_unavailable=current.id if step.mark_agent_unavailable else None,
            split=step.mutation is M.SPLIT_TASK,
            reasons=(f"mutation:{step.mutation.value}",),
        )
