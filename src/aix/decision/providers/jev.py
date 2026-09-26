"""Real Jev client on the TypeSafe SDK (PLAYBOOK §18.4). The only module that imports the SDK.

SDK surface verified by introspection of ``typesafe-sdk`` (see ADR-0019): ``AsyncTypeSafeClient
(api_key=, model=, retry=, timeout=)``, ``await client.system_one(state, questions, *, model=,
retry=, timeout=)`` returning ``SystemOneResponse{model, usage, answers}`` where answers are
``ChoiceAnswer{choice, confidence, probabilities}``, ``ScoreAnswer{score, confidence, legend,
probabilities}`` or ``NoulAnswer{noul}``. Questions are ``Choice(criteria: dict)``,
``Score(criteria: list)`` and ``Noul(criteria: {true, false})``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from pydantic import JsonValue
from typesafe_sdk import (
    AsyncTypeSafeClient,
    ChoiceAnswer,
    NoulAnswer,
    RetryPolicy,
    ScoreAnswer,
)
from typesafe_sdk import Choice as SdkChoice
from typesafe_sdk import Noul as SdkNoul
from typesafe_sdk import Score as SdkScore

from aix.decision.jev import JevAnswer, JevError, JevQuestion, JevResponse

API_KEY_ENV = "TYPESAFE_API_KEY"


def _to_sdk(q: JevQuestion) -> SdkChoice | SdkScore | SdkNoul:
    if q.kind == "choice":
        return SdkChoice(instructions=q.instructions, criteria=dict(q.criteria))
    if q.kind == "score":
        return SdkScore(instructions=q.instructions, criteria=list(q.levels))
    criteria: dict[str, Any] = {}
    if q.true_means is not None:
        criteria["true"] = q.true_means
    if q.false_means is not None:
        criteria["false"] = q.false_means
    return SdkNoul(instructions=q.instructions, criteria=criteria or None)  # type: ignore[arg-type]


def _from_sdk(ans: Any) -> JevAnswer:
    if isinstance(ans, ChoiceAnswer):
        return JevAnswer(
            kind="choice",
            choice=ans.choice,
            confidence=ans.confidence,
            probabilities=dict(ans.probabilities),
        )
    if isinstance(ans, ScoreAnswer):
        return JevAnswer(kind="score", score=ans.score, confidence=ans.confidence)
    if isinstance(ans, NoulAnswer):
        return JevAnswer(kind="noul", noul=ans.noul)
    raise JevError(f"unexpected answer type {type(ans).__name__}")


class TypeSafeJevClient:
    """:class:`~aix.decision.jev.JevClient` backed by ``AsyncTypeSafeClient``."""

    def __init__(
        self,
        *,
        sdk: Any | None = None,
        model: str | None = None,
        timeout_s: float = 3.0,
    ) -> None:
        if sdk is None:
            key = os.environ.get(API_KEY_ENV)
            if not key:
                raise JevError(f"{API_KEY_ENV} is not set")
            sdk = AsyncTypeSafeClient(
                api_key=key, model=model, retry=RetryPolicy(max_retries=1), timeout=timeout_s
            )
        self._sdk: Any = sdk
        self._model = model
        self._timeout = timeout_s

    async def system_one(
        self, state: dict[str, JsonValue] | str, questions: Mapping[str, JevQuestion]
    ) -> JevResponse:
        """Send ``state`` and ``questions`` to Jev; any SDK failure becomes ``JevError``."""
        try:
            resp: Any = await self._sdk.system_one(
                state,
                {name: _to_sdk(q) for name, q in questions.items()},
                model=self._model,
                timeout=self._timeout,
            )
            return JevResponse(
                answers={name: _from_sdk(a) for name, a in resp.answers.items()},
                model=resp.model,
                usage={
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens,
                },
            )
        except JevError:
            raise
        except Exception as exc:
            raise JevError(f"{type(exc).__name__}: {exc}") from exc
