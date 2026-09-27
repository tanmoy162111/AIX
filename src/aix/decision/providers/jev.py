"""Real Jev client on the TypeSafe SDK (PLAYBOOK §18.4). The only module that imports the SDK.

The key comes from ``TYPESAFE_API_KEY`` (TypeSafe direct) or, failing that, ``OPENROUTER_API_KEY``
(same SDK pointed at OpenRouter's System One endpoint, model ``jev-1.13``; ADR-0034).

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
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
OPENROUTER_MODEL = "jev-1.13"


def jev_key_env() -> str | None:
    """Name of the env var that supplies the Jev key (TypeSafe first, then OpenRouter), if any."""
    for name in (API_KEY_ENV, OPENROUTER_KEY_ENV):
        if os.environ.get(name):
            return name
    return None


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
        self.via = "typesafe"
        if sdk is None:
            env = jev_key_env()
            if env is None:
                raise JevError(f"{API_KEY_ENV} is not set (or {OPENROUTER_KEY_ENV} for OpenRouter)")
            base_url: str | None = None
            if env == OPENROUTER_KEY_ENV:
                self.via, base_url = "openrouter", OPENROUTER_BASE_URL
                model = model or OPENROUTER_MODEL
            sdk = AsyncTypeSafeClient(
                api_key=os.environ[env],
                model=model,
                retry=RetryPolicy(max_retries=1),
                timeout=timeout_s,
                base_url=base_url,
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
