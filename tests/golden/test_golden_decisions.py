"""Golden scenarios G3 (failure->retry), G4 (high risk), G8 (Jev provider), G10 (budget), §28."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
import yaml
from typer.testing import CliRunner

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep, load_scripts
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.cli.main import app
from aix.config.schema import AixConfig
from aix.core.orchestrator.executor import RunOutcome, execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.decision.jev import FakeJevClient, JevAnswer, JevError
from aix.decision.providers.jev_provider import JevDecisionProvider
from aix.decision.providers.rules import RulesProvider
from aix.decision.service import DecisionService
from aix.decision.thresholds import Thresholds
from aix.domain.enums import Capability, FailureClass, RetryMutation, RunStatus, TaskType
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.security.approvals import ensure_token, short_id
from aix.store.db import EventStore
from cli_env import hermetic
from verif_env import fast_config, write_fast_config

C = Capability
SCRIPTS = repos.FIXTURES / "agent_scripts"
runner = CliRunner()


async def run_tasks(
    repo: Path,
    scripts: list[FakeScript],
    build: Callable[[str], list[Task]],
    cfg: AixConfig,
    *,
    decisions: DecisionService | None = None,
) -> tuple[RunOutcome, EventStore]:
    registry = AdapterRegistry(cfg, builtin_ids=())
    registry.register(
        make_fake_entry(
            "fake-a", base_dir=SCRIPTS, scripts=scripts, capabilities={C.IMPLEMENT: 0.9}
        )
    )
    (repo / ".aix").mkdir(exist_ok=True)
    store = await EventStore.open(repo / ".aix" / "aix.db")
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    now = lambda: datetime.now(UTC)  # noqa: E731
    rec = RunRecorder(store, new_run(run_id, wm.root, "goal", cfg, now()), now)
    await rec.start()
    tasks = build(run_id)
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=tasks)
    await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
    with anyio.fail_after(180):
        outcome = await execute_graph(
            rec, wm, graph, registry=registry, config=cfg, decisions=decisions, backoff_scale=0
        )
    return outcome, store


def implement(run_id: str, title: str, deps: list[str] | None = None, **kw: object) -> Task:
    return Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title=title, goal=f"do {title}",
        type=TaskType.IMPLEMENT, required_capabilities=[C.IMPLEMENT], file_scope=["**"],
        depends_on=deps or [], verification=VerificationSpec(required=[K.BUILD, K.TESTS, K.LINT]),
        **kw,  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


# ---- G3: attempt 1 fails verification despite its claim; attempt 2 passes ----------------------


@pytest.mark.anyio
async def test_g3_failure_then_retry_with_failure_context(repo: Path) -> None:
    cfg = AixConfig()  # real toolchain, default rules provider
    scripts = load_scripts(SCRIPTS / "auth_retry.yaml")
    outcome, store = await run_tasks(repo, scripts, lambda r: [implement(r, "auth")], cfg)
    try:
        assert outcome.status is RunStatus.COMPLETED and outcome.tasks[0].attempts == 2
        task = (await store.get_tasks(outcome.run_id))[0]
        first, second = await store.get_attempts(task.id)
        assert second.mutation is RetryMutation.SAME_AGENT_WITH_FAILURE_CONTEXT

        result = await store.get_result(first.id)
        assert result is not None and result.claim == "Done. All tests pass."  # the false claim
        checks = {c.kind: c for c in await store.get_checks(first.id)}
        assert checks[K.TESTS].status == "failed" and checks[K.TESTS].metrics["tests_failed"] > 0

        d1, d2 = await store.get_decisions(outcome.run_id)
        assert d1.outcome.value == "retry" and "gate:required_check_failed" in d1.reason_codes
        assert d2.outcome.value == "accept"
        prev = d2.state["history"]["previous_failures"]  # type: ignore[index]
        assert prev == ["verification_failure:tests"]
        assert "Done. All tests pass." not in json.dumps(d1.state) + json.dumps(d2.state)
        with_tests = repos.git(repo, "show", f"{outcome.branch}:app/auth.py")
        assert with_tests.returncode == 0
    finally:
        await store.close()


# ---- G8: scripted Jev: confident accept, low confidence, gate beats Jev, outage ----------------


def jev(conf: float, choice: str = "accept") -> dict[str, JevAnswer]:
    return {
        "completion": JevAnswer(kind="choice", choice=choice, confidence=conf),
        "residual_risk": JevAnswer(kind="score", score=0.2, confidence=0.9),
        "warnings_blocking": JevAnswer(kind="noul", noul=0.05),
    }


PY = [sys.executable, "-c"]
NO_BAD = [*PY, "import pathlib,sys; sys.exit(1 if pathlib.Path('bad.txt').exists() else 0)"]


def step(files: dict[str, str]) -> FakeStep:
    return FakeStep(write_files=files)


def g8_config() -> AixConfig:
    cfg = fast_config()
    cmds = dict(cfg.verification.commands) | {K.TESTS: NO_BAD}
    return cfg.model_copy(
        update={"verification": cfg.verification.model_copy(update={"commands": cmds})}
    )


def g8_scripts() -> list[FakeScript]:
    def s(word: str, *steps: FakeStep) -> FakeScript:
        return FakeScript(
            match=FakeMatch(task_type="implement", prompt_contains=word), attempts=list(steps)
        )

    return [
        s("do one", step({"a.py": "a = 1\n"})),
        s("do two", step({"b.py": "b = 1\n"})),
        s("do three", step({"c.py": "c = 1\n", "bad.txt": "x\n"}), step({"c.py": "c = 2\n"})),
    ]


def chain(run_id: str) -> list[Task]:
    one = implement(run_id, "one")
    two = implement(run_id, "two", [one.id])
    return [one, two, implement(run_id, "three", [two.id])]


def jev_service(client: FakeJevClient) -> DecisionService:
    rules = RulesProvider()
    thresholds = Thresholds(AixConfig().decision.thresholds)
    return DecisionService(
        JevDecisionProvider(client, rules, thresholds), rules, policy_version="policy-v1"
    )


@pytest.mark.anyio
async def test_g8_jev_decisions_are_recorded_and_gates_beat_jev(repo: Path) -> None:
    client = FakeJevClient(sequence=[jev(0.95), jev(0.3, "fix_and_retry"), jev(0.99), jev(0.95)])
    outcome, store = await run_tasks(
        repo, g8_scripts(), chain, g8_config(), decisions=jev_service(client)
    )
    try:
        assert outcome.status is RunStatus.COMPLETED
        d0, d1, d2, d3 = await store.get_decisions(outcome.run_id)
        assert (
            d0.provider == "jev"
            and d0.outcome.value == "accept"
            and "jev:accept" in d0.reason_codes
        )
        # low confidence: rules decided, and both Jev's answers and the reason are recorded
        assert d1.outcome.value == "accept" and "jev:low_confidence" in d1.reason_codes
        assert d1.answers["completion"]["confidence"] == 0.3  # type: ignore[index]
        # a required check failed: Jev says accept at 0.99, the gate does not allow it
        assert d2.outcome.value == "retry" and d2.provider == "rules"
        assert "jev:outcome_not_allowed" in d2.reason_codes
        assert "gate:required_check_failed" in d2.gate_result.reason_codes
        assert d3.outcome.value == "accept" and d3.provider == "jev"
        assert len(client.calls) == 4  # every consultation reached the client
        assert all(len(d.inputs_hash) == 64 for d in (d0, d1, d2, d3))
        sent = json.dumps([c.state for c in client.calls])
        assert "Done" not in sent and "claim" not in sent  # control-plane facts only
    finally:
        await store.close()


@pytest.mark.anyio
async def test_g8_jev_outage_never_blocks_the_run(repo: Path) -> None:
    client = FakeJevClient(error=JevError("service down"))
    outcome, store = await run_tasks(
        repo, g8_scripts(), chain, g8_config(), decisions=jev_service(client)
    )
    try:
        assert outcome.status is RunStatus.COMPLETED
        records = await store.get_decisions(outcome.run_id)
        assert records and all("jev:unavailable" in d.reason_codes for d in records)
        assert all(d.provider == "rules" for d in records)
    finally:
        await store.close()


# ---- CLI scenarios: G4 and G10 -----------------------------------------------------------------


def write_scripts(d: Path, **overrides: dict[str, object]) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    steps: dict[str, dict[str, object]] = {
        "implement": {"write_files": {"feature.py": "def f():\n    return 1\n"}},
        "test": {"write_files": {"tests/test_feature.py": "def test_x():\n    assert True\n"}},
        "document": {"write_files": {"docs/feature.md": "# Feature\n"}},
    }
    steps.update(overrides)
    for name, body in steps.items():
        (d / f"{name}.yaml").write_text(
            yaml.safe_dump({"match": {"task_type": name}, "attempts": [body]})
        )
    return d


@pytest.fixture
def cli_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic(tmp_path, monkeypatch)
    monkeypatch.delenv("AIX_AGENT_CONTEXT", raising=False)
    proj = repos.materialize_sample_py(tmp_path / "cli")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    write_fast_config(proj)
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(write_scripts(tmp_path / "scripts")))
    return proj


def cli(repo: Path, *args: str, input: str | None = None) -> tuple[int, str]:
    r = runner.invoke(app, [*args, "--project", str(repo)], input=input)
    return r.exit_code, r.output


def test_g4_high_risk_waits_then_a_tty_approval_resumes(
    cli_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = cli(cli_repo, "run", "Deploy to production", "--json")
    doc = json.loads(out)
    assert code == 3 and doc["status"] == "waiting_approval"
    assert doc["failure"] is None and {t["status"] for t in doc["tasks"]} == {"created"}
    (apv,) = doc["pending_approvals"]
    intent = json.loads(cli(cli_repo, "plan", "show", doc["run_id"], "--json")[1])["intent"]
    assert intent["risk"] == "high"

    # not a TTY and no token: refused
    assert cli(cli_repo, "approve", apv)[0] == 2
    # a simulated TTY where the human types the short id back
    monkeypatch.setattr("aix.cli.approvals._isatty", lambda: True)
    code, out = cli(cli_repo, "approve", apv, input="wrong\n")
    assert code == 2 and "did not match" in out
    code, out = cli(cli_repo, "approve", apv, input=f"{short_id(apv)}\n")
    assert code == 0 and "granted" in out and "COMPLETED" in out
    status = json.loads(cli(cli_repo, "status", doc["run_id"], "--json")[1])
    assert status["status"] == "completed"
    assert {t["status"] for t in status["tasks"]} == {"completed"}


def test_g4_denying_the_plan_fails_the_run(cli_repo: Path) -> None:
    _, out = cli(cli_repo, "run", "Deploy to production", "--json")
    doc = json.loads(out)
    (apv,) = doc["pending_approvals"]
    code, text = cli(cli_repo, "deny", apv, "--token", ensure_token(), "--reason", "too risky")
    assert code == 1 and "FAILED" in text
    status = json.loads(cli(cli_repo, "status", doc["run_id"], "--json")[1])
    assert status["status"] == "failed"
    con = sqlite3.connect(cli_repo / ".aix" / "aix.db")
    try:
        (n,) = con.execute("SELECT count(*) FROM events WHERE type='attempt.created'").fetchone()
    finally:
        con.close()
    assert n == 0  # nothing was ever executed


def test_g10_budget_stops_the_run_with_exit_6(
    cli_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pricey = {
        "write_files": {"feature.py": "def f():\n    return 1\n"},
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.05},
    }
    monkeypatch.setenv(
        "AIX_FAKE_SCRIPTS", str(write_scripts(tmp_path / "pricey", implement=pricey))
    )
    code, out = cli(cli_repo, "run", "Add a retry option", "--budget-usd", "0.01", "--json")
    doc = json.loads(out)
    assert code == 6 and doc["exit_code"] == 6, out
    assert doc["status"] == "failed" and doc["failure"] == FailureClass.BUDGET_EXCEEDED.value
    con = sqlite3.connect(cli_repo / ".aix" / "aix.db")
    try:
        kinds = [r[0] for r in con.execute("SELECT type FROM events ORDER BY seq")]
        (budget,) = con.execute(
            "SELECT payload FROM events WHERE type='budget.exceeded'"
        ).fetchall()
        decision = con.execute("SELECT outcome FROM decisions WHERE point='budget'").fetchone()
    finally:
        con.close()
    assert "budget.exceeded" in kinds and json.loads(budget[0])["budget"] == "cost_usd"
    assert decision == ("stop",)
    assert [k for k in kinds if k != "artifact.created"][-1] == "run.failed"
