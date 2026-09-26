from __future__ import annotations

from typing import Any

import pytest
from typesafe_sdk import Choice, Noul, Score, SystemOneResponse

from aix.decision.jev import (
    FakeJevClient,
    JevAnswer,
    JevClient,
    JevError,
    JevQuestion,
    JevResponse,
    choice_q,
    noul_q,
    score_q,
)
from aix.decision.providers.jev import TypeSafeJevClient

pytestmark = pytest.mark.anyio

QUESTIONS = {
    "completion": choice_q("What should happen?", {"accept": "all good", "retry": "fix it"}),
    "risk": score_q("How risky?", ["none", "low", "high"]),
    "block": noul_q("Should warnings block?"),
}


def test_question_helpers_build_typed_questions() -> None:
    assert QUESTIONS["completion"].kind == "choice"
    assert QUESTIONS["completion"].criteria == {"accept": "all good", "retry": "fix it"}
    assert QUESTIONS["risk"].kind == "score" and QUESTIONS["risk"].levels == ["none", "low", "high"]
    assert QUESTIONS["block"].kind == "noul"
    with pytest.raises(ValueError):
        JevQuestion(kind="choice", instructions="x")  # a choice needs criteria
    with pytest.raises(ValueError):
        JevQuestion(kind="score", instructions="x")  # a score needs levels


async def test_fake_client_returns_scripted_answers_and_records_calls() -> None:
    fake = FakeJevClient(
        {
            "completion": JevAnswer(
                kind="choice",
                choice="accept",
                confidence=0.9,
                probabilities={"accept": 0.9, "retry": 0.1},
            ),
            "risk": JevAnswer(kind="score", score=0.4, confidence=0.8),
            "block": JevAnswer(kind="noul", noul=0.1),
        }
    )
    assert isinstance(fake, JevClient)
    resp = await fake.system_one({"task": {"type": "implement"}}, QUESTIONS)
    assert resp.answers["completion"].choice == "accept" and resp.answers["risk"].score == 0.4
    assert fake.calls[0].state == {"task": {"type": "implement"}}
    assert set(fake.calls[0].questions) == {"completion", "risk", "block"}


async def test_fake_client_can_raise_and_script_a_sequence() -> None:
    boom = FakeJevClient(error=JevError("down"))
    with pytest.raises(JevError):
        await boom.system_one({}, QUESTIONS)
    seq = FakeJevClient(sequence=[{"block": JevAnswer(kind="noul", noul=0.2)},
                                  {"block": JevAnswer(kind="noul", noul=0.9)}])  # fmt: skip
    q = {"block": QUESTIONS["block"]}
    assert (await seq.system_one({}, q)).answers["block"].noul == 0.2
    assert (await seq.system_one({}, q)).answers["block"].noul == 0.9


async def test_fake_client_omits_unscripted_questions() -> None:
    resp = await FakeJevClient({}).system_one({}, QUESTIONS)
    assert resp.answers == {}


class SdkStub:
    """Stands in for AsyncTypeSafeClient; records what the adapter passed."""

    def __init__(self, response: Any) -> None:
        self.response, self.seen = response, {}

    async def system_one(self, state: Any, questions: Any, **kw: Any) -> Any:
        self.seen = {"state": state, "questions": questions, "kw": kw}
        return self.response


async def test_real_client_translates_questions_and_answers() -> None:
    sdk_response = SystemOneResponse.model_validate({
        "model": "m1", "usage": {"input_tokens": 10, "output_tokens": 2},
        "answers": {
            "completion": {"type": "choice", "choice": "accept", "confidence": 0.93,
                           "probabilities": {"accept": 0.93, "retry": 0.07}},
            "risk": {"type": "score", "score": 1.2, "confidence": 0.7,
                     "legend": {0: "none", 1: "low", 2: "high"},
                     "probabilities": {0: 0.1, 1: 0.8, 2: 0.1}},
            "block": {"type": "noul", "noul": 0.05},
        },
    })  # fmt: skip
    stub = SdkStub(sdk_response)
    client = TypeSafeJevClient(sdk=stub, model="m1", timeout_s=2.0)
    resp = await client.system_one({"k": 1}, QUESTIONS)
    sent = stub.seen["questions"]
    assert isinstance(sent["completion"], Choice) and sent["completion"].criteria == {
        "accept": "all good", "retry": "fix it"}  # fmt: skip
    assert isinstance(sent["risk"], Score) and list(sent["risk"].criteria) == [
        "none",
        "low",
        "high",
    ]
    assert isinstance(sent["block"], Noul)
    assert stub.seen["state"] == {"k": 1} and stub.seen["kw"]["timeout"] == 2.0
    assert stub.seen["kw"]["model"] == "m1"
    a = resp.answers
    assert a["completion"].choice == "accept" and a["completion"].confidence == 0.93
    assert a["completion"].probabilities == {"accept": 0.93, "retry": 0.07}
    assert a["risk"].score == 1.2 and a["block"].noul == 0.05
    assert isinstance(resp, JevResponse) and resp.model == "m1"


async def test_real_client_wraps_sdk_errors_in_jev_error() -> None:
    class Failing:
        async def system_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("network down")

    with pytest.raises(JevError, match="network down"):
        await TypeSafeJevClient(sdk=Failing()).system_one({}, QUESTIONS)


def test_real_client_without_a_key_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    with pytest.raises(JevError, match="TYPESAFE_API_KEY"):
        TypeSafeJevClient()
