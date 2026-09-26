from __future__ import annotations

from collections import deque
from collections.abc import Mapping

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

import factories as f
from aix.domain import state
from aix.domain.enums import (
    RUN_TERMINAL,
    TASK_TERMINAL,
    RunStatus,
    TaskStatus,
)
from aix.domain.errors import IllegalTransition
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import RunEvent, TaskEvent

RID = new_id(IdPrefix.RUN)


# ---- table-level properties ------------------------------------------------


def _reachable[S, E](table: Mapping[tuple[S, E], S], start: S) -> set[S]:
    seen = {start}
    queue = deque([start])
    while queue:
        cur = queue.popleft()
        for (src, _), dst in table.items():
            if src == cur and dst not in seen:
                seen.add(dst)
                queue.append(dst)
    return seen


def test_task_table_covers_only_known_states() -> None:
    for (src, _), dst in state.TASK_TRANSITIONS.items():
        assert src in TaskStatus
        assert dst in TaskStatus


def test_all_task_states_reachable_from_created() -> None:
    assert _reachable(state.TASK_TRANSITIONS, TaskStatus.CREATED) == set(TaskStatus)


def test_all_run_states_reachable_from_created() -> None:
    assert _reachable(state.RUN_TRANSITIONS, RunStatus.CREATED) == set(RunStatus)


@pytest.mark.parametrize("status", [s for s in TaskStatus if s not in TASK_TERMINAL])
def test_every_non_terminal_task_state_can_reach_a_terminal_state(status: TaskStatus) -> None:
    assert _reachable(state.TASK_TRANSITIONS, status) & TASK_TERMINAL


@pytest.mark.parametrize("status", [s for s in RunStatus if s not in RUN_TERMINAL])
def test_every_non_terminal_run_state_can_reach_a_terminal_state(status: RunStatus) -> None:
    assert _reachable(state.RUN_TRANSITIONS, status) & RUN_TERMINAL


def test_terminal_states_have_no_outgoing_edges() -> None:
    assert not [k for k in state.TASK_TRANSITIONS if k[0] in TASK_TERMINAL]
    assert not [k for k in state.RUN_TRANSITIONS if k[0] in RUN_TERMINAL]


def test_blocked_only_leaves_via_cancel() -> None:
    out = {e for (s, e) in state.TASK_TRANSITIONS if s is TaskStatus.BLOCKED}
    assert out == {TaskEvent.CANCEL}


# ---- exhaustive (status x event) behavior -----------------------------------


@pytest.mark.parametrize("status", list(TaskStatus))
@pytest.mark.parametrize("event", list(TaskEvent))
def test_task_transition_is_total(status: TaskStatus, event: TaskEvent) -> None:
    t = f.task(RID, status=status)
    expected = state.TASK_TRANSITIONS.get((status, event))
    if expected is None:
        with pytest.raises(IllegalTransition):
            state.transition_task(t, event)
    else:
        assert state.transition_task(t, event).status is expected


@pytest.mark.parametrize("status", list(RunStatus))
@pytest.mark.parametrize("event", list(RunEvent))
def test_run_transition_is_total(status: RunStatus, event: RunEvent) -> None:
    finished = f.NOW if status in RUN_TERMINAL else None
    r = f.run(status=status, finished_at=finished)
    expected = state.RUN_TRANSITIONS.get((status, event))
    if expected is None:
        with pytest.raises(IllegalTransition):
            state.transition_run(r, event, at=f.NOW)
    else:
        out = state.transition_run(r, event, at=f.NOW)
        assert out.status is expected
        assert (out.finished_at is not None) == (expected in RUN_TERMINAL)


def test_transition_does_not_mutate_input() -> None:
    t = f.task(RID)
    out = state.transition_task(t, TaskEvent.DEPS_SATISFIED)
    assert t.status is TaskStatus.CREATED
    assert out.status is TaskStatus.READY
    assert out.id == t.id


# ---- the spec's named paths (§7) -------------------------------------------


def _walk(status: TaskStatus, *events: TaskEvent) -> TaskStatus:
    t = f.task(RID, status=status)
    for e in events:
        t = state.transition_task(t, e)
    return t.status


def test_task_happy_path() -> None:
    E = TaskEvent
    end = _walk(
        TaskStatus.CREATED,
        E.DEPS_SATISFIED, E.ASSIGN, E.START, E.EXECUTION_FINISHED, E.VERIFIED,
        E.ACCEPT, E.INTEGRATE, E.INTEGRATED,
    )  # fmt: skip
    assert end is TaskStatus.COMPLETED


@pytest.mark.parametrize("event", [TaskEvent.RETRY, TaskEvent.SWITCH_AGENT, TaskEvent.ESCALATE])
def test_retry_like_decisions_return_to_ready(event: TaskEvent) -> None:
    assert _walk(TaskStatus.DECIDING, event) is TaskStatus.READY


def test_ask_human_paths() -> None:
    E = TaskEvent
    assert _walk(TaskStatus.DECIDING, E.ASK_HUMAN, E.APPROVAL_GRANTED) is TaskStatus.READY
    assert _walk(TaskStatus.DECIDING, E.ASK_HUMAN, E.APPROVAL_DENIED) is TaskStatus.FAILED


@pytest.mark.parametrize("event", [TaskEvent.REJECT, TaskEvent.STOP])
def test_reject_and_stop_fail_the_task(event: TaskEvent) -> None:
    assert _walk(TaskStatus.DECIDING, event) is TaskStatus.FAILED


def test_failed_attempt_goes_to_deciding_without_verification() -> None:
    assert _walk(TaskStatus.RUNNING, TaskEvent.ATTEMPT_FAILED) is TaskStatus.DECIDING


def test_merge_conflict_returns_task_to_ready() -> None:
    assert _walk(TaskStatus.INTEGRATING, TaskEvent.MERGE_CONFLICT) is TaskStatus.READY
    assert _walk(TaskStatus.INTEGRATING, TaskEvent.STOP) is TaskStatus.FAILED  # ADR-0013


def test_no_eligible_agent_fails_a_ready_task() -> None:
    assert _walk(TaskStatus.READY, TaskEvent.NO_ELIGIBLE_AGENT) is TaskStatus.FAILED


@pytest.mark.parametrize("status", [s for s in TaskStatus if s not in TASK_TERMINAL])
def test_any_non_terminal_task_can_be_cancelled(status: TaskStatus) -> None:
    assert _walk(status, TaskEvent.CANCEL) is TaskStatus.CANCELLED


@pytest.mark.parametrize(
    "status",
    [s for s in TaskStatus if s not in TASK_TERMINAL and s is not TaskStatus.BLOCKED],
)
def test_any_live_task_can_be_blocked(status: TaskStatus) -> None:
    assert _walk(status, TaskEvent.DEPENDENCY_FAILED) is TaskStatus.BLOCKED


def test_run_paths() -> None:
    E = RunEvent
    r = f.run()
    for e in (E.START_PLANNING, E.PLANNED, E.START_EXECUTION, E.ALL_TASKS_TERMINAL, E.COMPLETE):
        r = state.transition_run(r, e, at=f.NOW)
    assert r.status is RunStatus.COMPLETED
    assert r.finished_at == f.NOW


def test_run_waiting_approval_round_trip_and_denial() -> None:
    E = RunEvent
    r = f.run(status=RunStatus.EXECUTING)
    r = state.transition_run(r, E.AWAIT_APPROVAL, at=f.NOW)
    assert state.transition_run(r, E.APPROVAL_GRANTED, at=f.NOW).status is RunStatus.EXECUTING
    assert state.transition_run(r, E.APPROVAL_DENIED, at=f.NOW).status is RunStatus.FAILED


def test_plan_review_can_pause_a_planned_run() -> None:
    r = f.run(status=RunStatus.PLANNED)
    r = state.transition_run(r, RunEvent.AWAIT_APPROVAL, at=f.NOW)
    assert r.status is RunStatus.WAITING_APPROVAL


# ---- state-machine (hypothesis) -------------------------------------------


class TaskMachine(RuleBasedStateMachine):
    """No sequence of events can produce an undefined state; terminal states absorb."""

    def __init__(self) -> None:
        super().__init__()
        self.task = f.task(RID)
        self.frozen_at: TaskStatus | None = None

    @rule(event=st.sampled_from(list(TaskEvent)))
    def fire(self, event: TaskEvent) -> None:
        before = self.task
        allowed = (before.status, event) in state.TASK_TRANSITIONS
        try:
            self.task = state.transition_task(before, event)
        except IllegalTransition:
            assert not allowed
            assert self.task == before
            return
        assert allowed
        if before.status in TASK_TERMINAL:
            raise AssertionError("terminal state was left")
        if self.task.status in TASK_TERMINAL and self.frozen_at is None:
            self.frozen_at = self.task.status

    @invariant()
    def status_is_defined(self) -> None:
        assert self.task.status in TaskStatus

    @invariant()
    def terminal_absorbs(self) -> None:
        if self.frozen_at is not None:
            assert self.task.status is self.frozen_at

    @precondition(lambda self: self.task.status is TaskStatus.BLOCKED)
    @rule(event=st.sampled_from([e for e in TaskEvent if e is not TaskEvent.CANCEL]))
    def blocked_never_runs(self, event: TaskEvent) -> None:
        with pytest.raises(IllegalTransition):
            state.transition_task(self.task, event)


TestTaskMachine = TaskMachine.TestCase
TestTaskMachine.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)


class RunMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.run = f.run()

    @rule(event=st.sampled_from(list(RunEvent)))
    def fire(self, event: RunEvent) -> None:
        before = self.run
        try:
            self.run = state.transition_run(before, event, at=f.NOW)
        except IllegalTransition:
            assert (before.status, event) not in state.RUN_TRANSITIONS
            return
        assert before.status not in RUN_TERMINAL

    @invariant()
    def finished_iff_terminal(self) -> None:
        assert (self.run.finished_at is not None) == (self.run.status in RUN_TERMINAL)


TestRunMachine = RunMachine.TestCase
TestRunMachine.settings = settings(max_examples=200, stateful_step_count=40, deadline=None)


# ---- blocked propagation ---------------------------------------------------


def test_failed_dependency_blocks_direct_and_transitive_dependents() -> None:
    a = f.task(RID, status=TaskStatus.FAILED)
    b = f.task(RID, depends_on=[a.id], status=TaskStatus.CREATED)
    c = f.task(RID, depends_on=[b.id], status=TaskStatus.CREATED)
    d = f.task(RID, status=TaskStatus.CREATED)
    g = f.graph(RID, [a, b, c, d])
    assert state.tasks_to_block(g.tasks) == [b.id, c.id]


def test_cancelled_dependency_blocks_too() -> None:
    a = f.task(RID, status=TaskStatus.CANCELLED)
    b = f.task(RID, depends_on=[a.id], status=TaskStatus.READY)
    assert state.tasks_to_block([a, b]) == [b.id]


def test_terminal_and_already_blocked_dependents_are_left_alone() -> None:
    a = f.task(RID, status=TaskStatus.FAILED)
    done = f.task(RID, depends_on=[a.id], status=TaskStatus.COMPLETED)
    blocked = f.task(RID, depends_on=[a.id], status=TaskStatus.BLOCKED)
    assert state.tasks_to_block([a, done, blocked]) == []


def test_completed_dependencies_do_not_block() -> None:
    a = f.task(RID, status=TaskStatus.COMPLETED)
    b = f.task(RID, depends_on=[a.id], status=TaskStatus.CREATED)
    assert state.tasks_to_block([a, b]) == []


@st.composite
def _graphs(draw: st.DrawFn) -> list[tuple[list[int], TaskStatus]]:
    n = draw(st.integers(min_value=1, max_value=8))
    spec: list[tuple[list[int], TaskStatus]] = []
    for i in range(n):
        deps = draw(st.lists(st.integers(0, max(i - 1, 0)), unique=True, max_size=3)) if i else []
        status = draw(st.sampled_from(list(TaskStatus)))
        spec.append((deps, status))
    return spec


@given(_graphs())
def test_after_propagation_no_live_task_has_a_dead_dependency(
    spec: list[tuple[list[int], TaskStatus]],
) -> None:
    ids = [new_id(IdPrefix.TASK) for _ in spec]
    tasks = [
        f.task(RID, id=ids[i], depends_on=[ids[d] for d in deps], status=status)
        for i, (deps, status) in enumerate(spec)
    ]
    to_block = set(state.tasks_to_block(tasks))
    dead = {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.BLOCKED}
    final = {t.id: (TaskStatus.BLOCKED if t.id in to_block else t.status) for t in tasks}
    for t in tasks:
        if final[t.id] in TASK_TERMINAL or final[t.id] is TaskStatus.BLOCKED:
            continue
        assert not any(final[d] in dead for d in t.depends_on)
    # only live tasks are ever selected, and each selection is a legal transition
    for t in tasks:
        if t.id in to_block:
            assert (t.status, TaskEvent.DEPENDENCY_FAILED) in state.TASK_TRANSITIONS
