from __future__ import annotations

import anyio
import pytest

from aix.decision.gates import GateFacts
from aix.decision.provider import ProviderAnswer, ProviderName
from aix.decision.service import DecisionService, canonical_hash
from aix.decision.state import DecisionState, TaskFacts
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P
from aix.domain.verification import VerificationReport

pytestmark = pytest.mark.anyio

STATE = DecisionState(task=TaskFacts(type="implement", risk="low", attempt=1, max_attempts=3))
FACTS = GateFacts(attempt=1, max_attempts=3)


class Stub:
    def __init__(self, name: ProviderName, outcome: O = O.ACCEPT, *, choice: str | None = None,
                 boom: bool = False, sleep: float = 0.0) -> None:  # fmt: skip
        self.name: ProviderName = name
        self.outcome, self.choice, self.boom, self.sleep = outcome, choice, boom, sleep
        self.calls = 0

    async def decide(self, point: P, state: DecisionState, allowed: set[O]) -> ProviderAnswer:
        self.calls += 1
        if self.sleep:
            await anyio.sleep(self.sleep)
        if self.boom:
            raise RuntimeError("provider exploded")
        return ProviderAnswer(
            outcome=self.outcome, choice=self.choice, reason_codes=[f"{self.name}:said"],
            questions={"q": "?"}, answers={"a": 1},
        )  # fmt: skip


def svc(provider: Stub, fallback: Stub | None = None, timeout: float = 5.0) -> DecisionService:
    return DecisionService(
        provider, fallback or Stub("rules", O.REJECT), policy_version="p1", timeout_s=timeout
    )


async def test_provider_answer_within_allowed_outcomes_is_recorded() -> None:
    rec = await svc(Stub("fake", O.ACCEPT)).decide(P.TASK_COMPLETION, "task_1", STATE, FACTS)
    assert rec.outcome is O.ACCEPT and rec.provider == "fake" and rec.subject == "task_1"
    assert rec.state == STATE.to_wire() and rec.questions == {"q": "?"} and rec.answers == {"a": 1}
    assert "fake:said" in rec.reason_codes and rec.gate_result.reason_codes == []


async def test_forced_gate_outcome_skips_the_provider() -> None:
    p = Stub("fake", O.ACCEPT)
    facts = GateFacts(actions=["push"], approval_required_for=["push"], attempt=1, max_attempts=3)
    rec = await svc(p).decide(P.TASK_COMPLETION, "t", STATE, facts)
    assert rec.outcome is O.ASK_HUMAN and p.calls == 0 and rec.provider == "rules"
    assert "gate:approval_required:push" in rec.reason_codes


async def test_provider_cannot_pick_an_outcome_the_gates_forbid() -> None:
    from aix.domain.enums import CheckKind as K
    from aix.domain.ids import IdPrefix, new_id
    from aix.domain.verification import Check, compute_overall

    bad = Check(
        id=new_id(IdPrefix.CHECK), kind=K.TESTS, status="failed", required=True, summary="s"
    )
    rep = VerificationReport(
        attempt_id=new_id(IdPrefix.ATTEMPT), checks=[bad], overall=compute_overall([bad])
    )
    rules = Stub("rules", O.RETRY)
    rec = await svc(Stub("fake", O.ACCEPT), rules).decide(
        P.TASK_COMPLETION, "t", STATE, GateFacts(report=rep, attempt=1, max_attempts=3)
    )
    assert rec.outcome is O.RETRY and rec.provider == "rules"
    assert (
        "fake:outcome_not_allowed" in rec.reason_codes
        and "gate:required_check_failed" in rec.reason_codes
    )


async def test_provider_error_and_timeout_fall_back_to_rules_with_a_reason() -> None:
    rec = await svc(Stub("jev", boom=True), Stub("rules", O.RETRY)).decide(
        P.TASK_COMPLETION, "t", STATE, FACTS
    )
    assert (
        rec.outcome is O.RETRY and rec.provider == "rules" and "jev:unavailable" in rec.reason_codes
    )
    slow = await svc(Stub("jev", sleep=2), Stub("rules", O.RETRY), timeout=0.1).decide(
        P.TASK_COMPLETION, "t", STATE, FACTS
    )
    assert slow.outcome is O.RETRY and "jev:unavailable" in slow.reason_codes


async def test_choose_needs_a_valid_candidate() -> None:
    ok = await svc(Stub("fake", O.CHOOSE, choice="b")).decide(
        P.ROUTING, "t", STATE, FACTS, candidates=["a", "b"]
    )
    assert ok.outcome is O.CHOOSE and ok.choice == "b"
    bad = await svc(
        Stub("fake", O.CHOOSE, choice="zzz"), Stub("rules", O.CHOOSE, choice="a")
    ).decide(P.ROUTING, "t", STATE, FACTS, candidates=["a", "b"])
    assert bad.choice == "a" and "fake:invalid_choice" in bad.reason_codes


async def test_last_resort_when_even_the_fallback_is_unusable() -> None:
    rec = await svc(Stub("fake", boom=True), Stub("rules", O.STOP)).decide(
        P.TASK_COMPLETION, "t", STATE, FACTS
    )
    assert rec.outcome is O.ASK_HUMAN and "decision:no_valid_answer" in rec.reason_codes


def test_inputs_hash_is_canonical_and_sensitive() -> None:
    a = canonical_hash({"x": 1, "y": [1, 2]}, {"q": "?"}, "p1")
    assert a == canonical_hash({"y": [1, 2], "x": 1}, {"q": "?"}, "p1")  # key order irrelevant
    assert a != canonical_hash({"x": 2, "y": [1, 2]}, {"q": "?"}, "p1")
    assert a != canonical_hash({"x": 1, "y": [1, 2]}, {"q": "?"}, "p2")
    assert len(a) == 64


async def test_record_hash_matches_its_inputs() -> None:
    rec = await svc(Stub("fake")).decide(P.TASK_COMPLETION, "t", STATE, FACTS)
    assert rec.inputs_hash == canonical_hash(rec.state, rec.questions, "p1")
