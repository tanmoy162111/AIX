"""Decision-driven retries, mutations, escalation, budget and approvals (M5.9-M5.11, §18-§19)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig, BudgetConfig, ExecutionConfig
from aix.core.orchestrator.executor import RunOutcome, execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.decision.eval import replay_records
from aix.decision.providers.rules import RulesProvider
from aix.domain.enums import (
    Capability,
    FailureClass,
    RetryMutation,
    RunStatus,
    TaskStatus,
    TaskType,
)
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.security.policy import Policy
from aix.store.db import EventStore
from verif_env import fast_config

pytestmark = pytest.mark.anyio
C = Capability
PYPATH = [sys.executable, "-c"]
# `tests` fails only when bad.txt exists in the attempt worktree: a controllable verification that
# passes at baseline (so it is not downgraded to pre_existing).
NO_BAD = [*PYPATH, "import pathlib,sys; sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)"]
GOOD = {"feature.py": "x = 1\n"}
OTHER = {"other.py": "y = 2\n"}
BAD = {"feature.py": "x = 1\n", "bad.txt": "boom\n"}


def step(files: dict[str, str] | None = None, **kw: object) -> FakeStep:
    return FakeStep(write_files=files or {}, **kw)  # type: ignore[arg-type]


def script(*attempts: FakeStep, contains: str | None = None) -> FakeScript:
    return FakeScript(
        match=FakeMatch(task_type="implement", prompt_contains=contains), attempts=list(attempts)
    )


def config(**kw: object) -> AixConfig:
    cfg = fast_config(
        execution=ExecutionConfig(escalation_ladder=["stronger_model", "different_agent", "human"]),
        **kw,
    )  # type: ignore[arg-type]
    commands = dict(cfg.verification.commands) | {K.TESTS: NO_BAD}
    return cfg.model_copy(
        update={"verification": cfg.verification.model_copy(update={"commands": commands})}
    )


def implement_task(
    run_id: str, title: str = "implement", deps: list[str] | None = None, **kw: object
) -> Task:
    return Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title=title, goal=f"do {title}",
        type=TaskType.IMPLEMENT, required_capabilities=[C.IMPLEMENT], file_scope=["**"],
        depends_on=deps or [], verification=VerificationSpec(required=[K.TESTS]),
        **kw,  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    proj = repos.materialize_sample_py(tmp_path / "proj")
    (proj / ".aix").mkdir()
    return proj


async def run_graph(
    repo: Path,
    agents: list[tuple[str, dict[str, object]]],
    build_tasks,  # type: ignore[no-untyped-def]
    cfg: AixConfig | None = None,
) -> tuple[RunOutcome, EventStore, list[Task]]:
    cfg = cfg or config()
    registry = AdapterRegistry(cfg, builtin_ids=())
    for agent_id, kw in agents:
        registry.register(make_fake_entry(agent_id, **kw))  # type: ignore[arg-type]
    store = await EventStore.open(repo / ".aix" / "aix.db")
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    now = lambda: datetime.now(UTC)  # noqa: E731
    rec = RunRecorder(store, new_run(run_id, wm.root, "goal", cfg, now()), now)
    await rec.start()
    tasks = build_tasks(run_id)
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=tasks)
    await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
    with anyio.fail_after(120):
        outcome = await execute_graph(
            rec, wm, graph, registry=registry, config=cfg, backoff_scale=0.0
        )
    return outcome, store, tasks


AGENT_A = {C.IMPLEMENT: 0.9, C.TEST: 0.9}
AGENT_B = {C.IMPLEMENT: 0.5, C.TEST: 0.5}


async def attempts_of(store: EventStore, run_id: str, task: Task):  # type: ignore[no-untyped-def]
    return await store.get_attempts(task.id)


async def test_failed_verification_retries_with_failure_context_then_passes(repo: Path) -> None:
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": [script(step(BAD), step(GOOD))]})]
    outcome, store, (task,) = await run_graph(repo, agents, lambda r: [implement_task(r)])
    try:
        assert outcome.status is RunStatus.COMPLETED and outcome.tasks[0].attempts == 2
        first, second = await store.get_attempts(task.id)
        assert (
            first.mutation is None
            and second.mutation is RetryMutation.SAME_AGENT_WITH_FAILURE_CONTEXT
        )
        decisions = await store.get_decisions(outcome.run_id)
        assert [d.outcome.value for d in decisions] == ["retry", "accept"]
        assert all(d.point.value == "task_completion" for d in decisions)
        assert "gate:required_check_failed" in decisions[0].reason_codes
        assert len(decisions[0].inputs_hash) == 64
    finally:
        await store.close()


async def test_repeated_failure_switches_to_another_agent(repo: Path) -> None:
    agents = [
        ("fake-a", {"capabilities": AGENT_A, "scripts": [script(step(BAD))]}),
        ("fake-b", {"capabilities": AGENT_B, "scripts": [script(step(GOOD))]}),
    ]
    outcome, store, (task,) = await run_graph(repo, agents, lambda r: [implement_task(r)])
    try:
        assert outcome.status is RunStatus.COMPLETED
        attempts = await store.get_attempts(task.id)
        assert [a.agent_id for a in attempts] == ["fake-a", "fake-a", "fake-b"]
        assert attempts[2].mutation is RetryMutation.SWITCH_AGENT
    finally:
        await store.close()


async def test_exhausted_attempts_climb_the_ladder_to_a_stronger_model_then_a_human(
    repo: Path,
) -> None:
    kw: dict[str, object] = {
        "capabilities": AGENT_A,
        "models": ["small", "big"],
        "default_model": "small",
        "scripts": [script(step(BAD))],
    }
    agents = [("fake-a", kw)]
    outcome, store, (task,) = await run_graph(repo, agents, lambda r: [implement_task(r)])
    try:
        attempts = await store.get_attempts(task.id)
        assert attempts[-1].model == "big"  # escalated to the stronger model of the same agent
        assert outcome.status is RunStatus.WAITING_APPROVAL and outcome.exit_code == 3
        assert outcome.tasks[0].status is TaskStatus.WAITING_APPROVAL
        assert len(outcome.pending_approvals) == 1
        approvals = await store.list_approvals("pending")
        assert approvals and approvals[0].subject == task.id
        decisions = await store.get_decisions(outcome.run_id)
        assert "escalate" in [d.outcome.value for d in decisions]
        assert decisions[-1].outcome.value in ("ask_human", "escalate")
    finally:
        await store.close()


async def test_rate_limit_retries_do_not_consume_attempts(repo: Path) -> None:
    limited = step(exit_code=1, stderr="429 rate limit exceeded", claim=None)
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": [script(limited, step(GOOD))]})]
    outcome, store, (task,) = await run_graph(
        repo, agents, lambda r: [implement_task(r, max_attempts=1)]
    )
    try:
        assert outcome.status is RunStatus.COMPLETED and outcome.tasks[0].attempts == 2
        _, a2 = await store.get_attempts(task.id)
        assert a2.mutation is RetryMutation.WAIT_AND_RETRY
    finally:
        await store.close()


async def test_auth_failure_marks_the_agent_unavailable_for_the_rest_of_the_run(repo: Path) -> None:
    denied = step(exit_code=1, stderr="401 Unauthorized", claim=None)
    agents = [
        ("fake-a", {"capabilities": AGENT_A, "scripts": [script(denied)]}),
        ("fake-b", {"capabilities": AGENT_B, "scripts": [script(step(GOOD), step(OTHER))]}),
    ]

    def tasks(run_id: str) -> list[Task]:
        first = implement_task(run_id, "first")
        return [first, implement_task(run_id, "second", [first.id])]

    outcome, store, _ = await run_graph(repo, agents, tasks)
    try:
        assert outcome.status is RunStatus.COMPLETED
        assert [t.agent_id for t in outcome.tasks] == ["fake-b", "fake-b"]  # a is never tried again
    finally:
        await store.close()


async def test_timeout_splits_the_task_and_dependents_wait_for_the_last_part(repo: Path) -> None:
    hang = step(sleep_s=30)
    parts = [step({f"p{i}.py": f"v = {i}\n"}) for i in (1, 2, 3)]
    scripts = [
        script(step({"after.py": "z = 1\n"}), contains="do after"),
        script(*parts, contains="part "),  # each subtask writes its own file
        script(hang),  # the original prompt hangs
    ]
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": scripts})]

    def tasks(run_id: str) -> list[Task]:
        big = implement_task(run_id, "big")
        return [big, implement_task(run_id, "after", [big.id])]

    cfg = config().model_copy(
        update={"execution": ExecutionConfig(escalation_ladder=[], attempt_timeout_s=1)}
    )
    outcome, store, (big, _after) = await run_graph(repo, agents, tasks, cfg)
    try:
        assert outcome.status is RunStatus.COMPLETED, outcome
        titles = [t.title for t in outcome.tasks]
        assert titles[0] == "after" or "big (part 1/3)" in titles
        assert {t.status for t in outcome.tasks} == {TaskStatus.COMPLETED}
        assert sum(1 for t in outcome.tasks if t.title.startswith("big (part")) == 3
        assert all(t.task_id != big.id for t in outcome.tasks)  # the original is superseded
        original = next(t for t in await store.get_tasks(outcome.run_id) if t.id == big.id)
        assert original.status is TaskStatus.CANCELLED
    finally:
        await store.close()


async def test_budget_overrun_stops_the_run_with_exit_6(repo: Path) -> None:
    pricey = step(GOOD, usage={"input_tokens": 1, "output_tokens": 1, "cost_usd": 0.05})  # type: ignore[arg-type]
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": [script(pricey)]})]

    def tasks(run_id: str) -> list[Task]:
        first = implement_task(run_id, "first")
        return [first, implement_task(run_id, "second", [first.id])]

    cfg = config().model_copy(update={"budget": BudgetConfig(max_cost_usd_per_run=0.01)})
    outcome, store, _ = await run_graph(repo, agents, tasks, cfg)
    try:
        assert (
            outcome.status is RunStatus.FAILED and outcome.failure is FailureClass.BUDGET_EXCEEDED
        )
        assert outcome.exit_code == 6
        (exceeded,) = await store.events(run_id=outcome.run_id, types=["budget.exceeded"])
        assert exceeded.payload.budget == "cost_usd"  # type: ignore[attr-defined]
        budget = [d for d in await store.get_decisions(outcome.run_id) if d.point.value == "budget"]
        assert budget and budget[0].outcome.value == "stop"
        assert outcome.tasks[1].status is not TaskStatus.COMPLETED  # nothing started after the stop
    finally:
        await store.close()


async def test_recorded_decisions_replay_identically_through_the_rules_provider(repo: Path) -> None:
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": [script(step(BAD), step(GOOD))]})]
    outcome, store, _ = await run_graph(repo, agents, lambda r: [implement_task(r)])
    try:
        records = await store.get_decisions(outcome.run_id)
        assert records
        assert (
            await replay_records(
                RulesProvider(), records, policy_version=Policy(fast_config().security).hash
            )
            == []
        )
    finally:
        await store.close()


async def test_gated_control_plane_command_is_refused_and_recorded(repo: Path) -> None:
    """A verification command that would deploy needs approval, so it is refused, not run."""
    marker = repo / "ran.txt"
    cfg = config()
    deploy = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('x')", "deploy"]
    commands = dict(cfg.verification.commands) | {K.TESTS: deploy}
    cfg = cfg.model_copy(
        update={"verification": cfg.verification.model_copy(update={"commands": commands})}
    )
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": [script(step(GOOD))]})]
    outcome, store, _ = await run_graph(repo, agents, lambda r: [implement_task(r)], cfg)
    try:
        assert not marker.exists()  # never executed
        decisions = [
            d for d in await store.get_decisions(outcome.run_id) if d.point.value == "tool_risk"
        ]
        assert decisions and decisions[0].outcome.value == "ask_human"
        assert "gate:approval_required:deploy" in decisions[0].reason_codes
        report = (await store.events(run_id=outcome.run_id, types=["verification.completed"]))[0]
        tests = next(c for c in report.payload.report.checks if c.kind is K.TESTS)  # type: ignore[attr-defined]
        assert tests.status == "error" and "command refused" in tests.summary
    finally:
        await store.close()


async def test_ordinary_commands_pass_without_tool_risk_decisions(repo: Path) -> None:
    agents = [("fake-a", {"capabilities": AGENT_A, "scripts": [script(step(GOOD))]})]
    outcome, store, _ = await run_graph(repo, agents, lambda r: [implement_task(r)])
    try:
        assert outcome.status is RunStatus.COMPLETED
        assert all(d.point.value != "tool_risk" for d in await store.get_decisions(outcome.run_id))
    finally:
        await store.close()
