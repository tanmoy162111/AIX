"""Decision evaluation harness (PLAYBOOK §18.7): labeled cases, accuracy, confusion, calibration."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Sequence
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, JsonValue

from aix.decision.gates import BASE_OUTCOMES
from aix.decision.provider import DecisionProvider, ProviderAnswer
from aix.decision.service import canonical_hash
from aix.decision.state import state_from_wire
from aix.domain.base import DomainModel
from aix.domain.decisions import DecisionRecord
from aix.domain.enums import DecisionOutcome, DecisionPoint

BINS = (0.0, 0.5, 0.7, 0.85, 0.95, 1.0001)


class EvalCase(DomainModel):
    """A labeled decision: the state a provider sees and the outcome it should choose."""

    id: str
    point: DecisionPoint
    state: dict[str, JsonValue]
    expected: DecisionOutcome
    allowed: list[DecisionOutcome] | None = None
    """Outcomes the gates would leave open; defaults to the point's full set."""
    tags: list[str] = Field(default_factory=list)


class CaseFailure(DomainModel):
    id: str
    expected: str
    actual: str
    reasons: list[str]


class CalibrationBin(DomainModel):
    low: float
    high: float
    n: int
    accuracy: float


class EvalReport(DomainModel):
    total: int
    correct: int
    accuracy: float
    per_point: dict[str, dict[str, float]]
    adversarial_accuracy: float | None
    confusion: dict[str, dict[str, int]]
    calibration: list[CalibrationBin]
    failures: list[CaseFailure]


def load_cases(path: Path) -> list[EvalCase]:
    """Load one YAML file or every ``*.yaml`` in a directory (sorted). Ids must be unique."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    cases: list[EvalCase] = []
    for f in files:
        doc: Any = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        cases += [EvalCase.model_validate(item) for item in doc]
    ids = [c.id for c in cases]
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"duplicate case ids: {', '.join(dupes)}")
    return cases


def confidence_of(answer: ProviderAnswer) -> float | None:
    """The provider's stated confidence for its decisive question, if it recorded one."""
    for name in ("completion", "mutation", "agent"):
        raw = answer.answers.get(name)
        conf = raw.get("confidence") if isinstance(raw, dict) else None
        if isinstance(conf, (int, float)):
            return float(conf)
    return None


async def run_eval(provider: DecisionProvider, cases: Sequence[EvalCase]) -> EvalReport:
    """Run ``provider`` over ``cases``. Provider errors count as wrong (``error:<type>``)."""
    correct = 0
    per_point: dict[str, list[bool]] = defaultdict(list)
    adversarial: list[bool] = []
    confusion: dict[str, Counter[str]] = defaultdict(Counter)
    failures: list[CaseFailure] = []
    cal: list[tuple[float, bool]] = []
    for case in cases:
        allowed = set(case.allowed) if case.allowed else set(BASE_OUTCOMES[case.point])
        reasons: list[str] = []
        try:
            ans = await provider.decide(case.point, state_from_wire(case.state), allowed)
            actual = ans.outcome.value
            reasons = ans.reason_codes
            conf = confidence_of(ans)
        except Exception as exc:
            actual, conf = f"error:{type(exc).__name__}", None
        ok = actual == case.expected.value
        correct += ok
        per_point[case.point.value].append(ok)
        if "adversarial" in case.tags:
            adversarial.append(ok)
        confusion[case.expected.value][actual] += 1
        if conf is not None:
            cal.append((conf, ok))
        if not ok:
            failures.append(
                CaseFailure(
                    id=case.id, expected=case.expected.value, actual=actual, reasons=reasons
                )
            )
    bins: list[CalibrationBin] = []
    for lo, hi in pairwise(BINS):
        hit = [ok for c, ok in cal if lo <= c < hi]
        if hit:
            bins.append(
                CalibrationBin(low=lo, high=min(hi, 1.0), n=len(hit), accuracy=sum(hit) / len(hit))
            )
    total = len(cases)
    return EvalReport(
        total=total,
        correct=correct,
        accuracy=correct / total if total else 0.0,
        per_point={
            p: {"n": float(len(v)), "accuracy": sum(v) / len(v)}
            for p, v in sorted(per_point.items())
        },
        adversarial_accuracy=(sum(adversarial) / len(adversarial)) if adversarial else None,
        confusion={e: dict(c) for e, c in sorted(confusion.items())},
        calibration=bins,
        failures=failures,
    )


class ReplayMismatch(DomainModel):
    decision_id: str
    recorded: str
    replayed: str
    hash_ok: bool


async def replay_records(
    provider: DecisionProvider, records: Sequence[DecisionRecord], *, policy_version: str
) -> list[ReplayMismatch]:
    """Re-run ``provider`` on recorded states; returns records whose outcome or hash differ.

    Records decided by the gates alone (forced outcomes) or by ``human`` are skipped: they have no
    provider judgment to replay.
    """
    out: list[ReplayMismatch] = []
    for rec in records:
        hash_ok = rec.inputs_hash == canonical_hash(rec.state, rec.questions, policy_version)
        if rec.gate_result.forced_outcome is not None or rec.provider == "human":
            if not hash_ok:
                out.append(
                    ReplayMismatch(
                        decision_id=rec.id, recorded=rec.outcome.value, replayed="-", hash_ok=False
                    )
                )
            continue
        allowed = set(rec.gate_result.allowed_outcomes)
        ans = await provider.decide(rec.point, state_from_wire(rec.state), allowed)
        if ans.outcome is not rec.outcome or not hash_ok:
            out.append(
                ReplayMismatch(
                    decision_id=rec.id,
                    recorded=rec.outcome.value,
                    replayed=ans.outcome.value,
                    hash_ok=hash_ok,
                )
            )
    return out
