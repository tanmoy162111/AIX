from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aix.cli.main import app
from aix.config.schema import AixConfig
from aix.decision.eval import EvalCase, load_cases, replay_records, run_eval
from aix.decision.jev import FakeJevClient, JevAnswer
from aix.decision.providers.jev_provider import JevDecisionProvider
from aix.decision.providers.rules import RulesProvider
from aix.decision.service import DecisionService
from aix.decision.state import DecisionState, state_from_wire
from aix.decision.thresholds import Thresholds
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P

pytestmark = pytest.mark.anyio
CASES = Path(__file__).resolve().parents[2] / "decision" / "eval_cases"
runner = CliRunner()


def test_shipped_case_files_meet_the_spec_minimums() -> None:
    cases = load_cases(CASES)
    by_point = {
        p: [c for c in cases if c.point is p] for p in (P.TASK_COMPLETION, P.FAILURE_TRIAGE)
    }
    assert len(by_point[P.TASK_COMPLETION]) >= 40 and len(by_point[P.FAILURE_TRIAGE]) >= 20
    assert any("adversarial" in c.tags for c in cases)
    assert len({c.id for c in cases}) == len(cases)


async def test_rules_hit_100_percent_on_their_own_table() -> None:
    report = await run_eval(RulesProvider(), load_cases(CASES))
    assert report.failures == [] and report.accuracy == 1.0
    assert report.adversarial_accuracy == 1.0
    assert set(report.per_point) == {"task_completion", "failure_triage"}


def test_state_round_trips_through_the_wire_form() -> None:
    for case in load_cases(CASES):
        st = state_from_wire(case.state)
        assert st.to_wire() == case.state, case.id


async def test_wrong_labels_are_reported_with_a_confusion_matrix() -> None:
    good = load_cases(CASES)[0]
    bad = good.model_copy(update={"id": "x", "expected": O.REJECT})
    report = await run_eval(RulesProvider(), [good, bad])
    assert report.correct == 1 and report.accuracy == 0.5
    real = good.expected.value
    assert report.confusion == {real: {real: 1}, "reject": {real: 1}}
    assert report.failures[0].id == "x" and report.failures[0].actual == real


async def test_provider_errors_count_as_wrong_not_as_crashes() -> None:
    class Boom:
        name = "rules"

        async def decide(self, *a: object) -> object:
            raise RuntimeError("x")

    report = await run_eval(Boom(), load_cases(CASES)[:2])  # type: ignore[arg-type]
    assert report.correct == 0 and report.failures[0].actual == "error:RuntimeError"


def test_load_cases_rejects_duplicate_ids(tmp_path: Path) -> None:
    case = load_cases(CASES)[0].model_dump(mode="json")
    (tmp_path / "a.yaml").write_text(json.dumps([case, case]))
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(tmp_path)


async def test_jev_provider_with_calibration_bins() -> None:
    cases = [c for c in load_cases(CASES) if c.point is P.TASK_COMPLETION][:6]
    client = FakeJevClient(
        {
            "completion": JevAnswer(kind="choice", choice="accept", confidence=0.95),
            "residual_risk": JevAnswer(kind="score", score=0.2, confidence=0.9),
            "warnings_blocking": JevAnswer(kind="noul", noul=0.05),
        }
    )
    jev = JevDecisionProvider(client, RulesProvider(), Thresholds(AixConfig().decision.thresholds))
    report = await run_eval(jev, cases)
    assert report.calibration and report.calibration[0].n == len(cases)
    assert 0.0 <= report.calibration[0].accuracy <= 1.0


async def test_replay_flags_outcome_and_hash_drift() -> None:
    svc = DecisionService(RulesProvider(), RulesProvider(), policy_version="p1")
    case = next(c for c in load_cases(CASES) if c.id.startswith("tc-001"))
    from aix.decision.gates import GateFacts

    rec = await svc.decide(
        P.TASK_COMPLETION, "t", state_from_wire(case.state), GateFacts(attempt=1, max_attempts=3)
    )
    assert await replay_records(RulesProvider(), [rec], policy_version="p1") == []
    drift = await replay_records(
        RulesProvider(), [rec.model_copy(update={"outcome": O.REJECT})], policy_version="p1"
    )
    assert drift[0].recorded == "reject" and drift[0].replayed == "accept"
    wrong_policy = await replay_records(RulesProvider(), [rec], policy_version="p2")
    assert wrong_policy and wrong_policy[0].hash_ok is False


def test_cli_rules_passes_and_fail_under_gates() -> None:
    r = runner.invoke(
        app,
        [
            "dev",
            "eval-decisions",
            "--provider",
            "rules",
            "--fail-under",
            "1.0",
            "--cases",
            str(CASES),
        ],
    )
    assert r.exit_code == 0 and "70/70" in r.output and "100.0%" in r.output


def test_cli_fail_under_fails_on_a_bad_label(tmp_path: Path) -> None:
    case = load_cases(CASES)[0].model_dump(mode="json")
    case["expected"] = "reject"
    (tmp_path / "c.yaml").write_text(json.dumps([case]))
    r = runner.invoke(
        app, ["dev", "eval-decisions", "--cases", str(tmp_path), "--fail-under", "1.0"]
    )
    assert r.exit_code == 1 and "FAIL" in r.output
    ok = runner.invoke(app, ["dev", "eval-decisions", "--cases", str(tmp_path)])
    assert ok.exit_code == 0  # no threshold given


def test_cli_jev_needs_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIX_LIVE", raising=False)
    r = runner.invoke(app, ["dev", "eval-decisions", "--provider", "jev", "--cases", str(CASES)])
    assert r.exit_code == 5 and "AIX_LIVE" in r.output
    assert runner.invoke(app, ["dev", "eval-decisions", "--provider", "nope"]).exit_code == 2


def test_cli_json_and_save(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    r = runner.invoke(
        app, ["dev", "eval-decisions", "--cases", str(CASES), "--json", "--save", str(out)]
    )
    doc = json.loads(r.output)
    assert doc["total"] == 70 and json.loads(out.read_text())["accuracy"] == 1.0
    assert isinstance(DecisionState(), DecisionState) and EvalCase is not None
