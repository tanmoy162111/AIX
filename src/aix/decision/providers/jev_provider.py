"""``jev`` decision provider: typed questions over compact facts, rules as the fallback (§18.4)."""

from __future__ import annotations

from typing import cast

from pydantic import JsonValue

from aix.decision import questions as Q
from aix.decision.jev import JevAnswer, JevClient, JevQuestion
from aix.decision.provider import DecisionProvider, ProviderAnswer, ProviderName
from aix.decision.questions import mapping as M
from aix.decision.state import DecisionState
from aix.decision.thresholds import Thresholds
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint as P


def _dump(answers: dict[str, JevAnswer]) -> dict[str, JsonValue]:
    return {
        k: cast(JsonValue, v.model_dump(mode="json", exclude_none=True)) for k, v in answers.items()
    }


def _dump_q(questions: dict[str, JevQuestion]) -> dict[str, JsonValue]:
    return {k: cast(JsonValue, v.model_dump(mode="json")) for k, v in questions.items()}


class JevDecisionProvider:
    """Asks Jev for the points that have a catalog entry; everything else goes to ``rules``.

    Contract: a Jev transport failure raises ``JevError`` (the Decision Service records
    ``jev:unavailable`` and falls back). When Jev answers but is not confident enough, or answers
    incompletely, the *rules* provider decides and the answer keeps Jev's questions/answers and a
    reason code, so both are visible in the decision record (§18.5).
    """

    name: ProviderName = "jev"

    def __init__(self, client: JevClient, rules: DecisionProvider, thresholds: Thresholds) -> None:
        self._client = client
        self._rules = rules
        self._th = thresholds

    async def _defer(
        self,
        point: P,
        state: DecisionState,
        allowed: set[O],
        reasons: list[str],
        questions: dict[str, JevQuestion],
        answers: dict[str, JevAnswer],
    ) -> ProviderAnswer:
        base = await self._rules.decide(point, state, allowed)
        return base.model_copy(
            update={
                "reason_codes": [*reasons, *base.reason_codes],
                "questions": _dump_q(questions),
                "answers": _dump(answers),
            }
        )

    async def decide(self, point: P, state: DecisionState, allowed: set[O]) -> ProviderAnswer:
        """See the class contract."""
        questions = self._questions(point, state)
        if not questions:
            base = await self._rules.decide(point, state, allowed)
            return base.model_copy(
                update={"reason_codes": ["jev:not_applicable", *base.reason_codes]}
            )
        resp = await self._client.system_one(state.to_wire(), questions)
        answers = resp.answers
        q_json = _dump_q(questions)
        a_json = _dump(answers)
        meta: dict[str, JsonValue] = {"model": resp.model, "usage": dict(resp.usage)}

        def answer(outcome: O, reasons: list[str], choice: str | None = None) -> ProviderAnswer:
            return ProviderAnswer(
                outcome=outcome,
                choice=choice,
                reason_codes=reasons,
                questions=q_json,
                answers=a_json,
                meta=meta,
            )

        if point is P.TASK_COMPLETION:
            outcome, reasons = M.map_task_completion(answers, state, self._th)
        elif point is P.FAILURE_TRIAGE:
            f = state.failure
            outcome, reasons, mutation, refined = M.map_failure_triage(
                answers, state, self._th, f.mutations if f else []
            )
            if outcome is not None:
                meta["mutation"] = mutation
                meta["refined_class"] = refined
        elif point is P.TOOL_RISK:
            outcome, reasons = M.map_tool_risk(answers, self._th)
        elif point is P.PLAN_REVIEW:
            outcome, reasons = M.map_plan_review(answers, self._th)
        else:  # routing
            picked, reasons = M.map_routing(answers, self._th)
            if picked is None or not state.routing or picked not in state.routing.candidates:
                return await self._defer(
                    point, state, allowed, reasons or ["jev:invalid_choice"], questions, answers
                )
            return answer(O.CHOOSE, reasons, picked)
        if outcome is None:
            return await self._defer(point, state, allowed, reasons, questions, answers)
        return answer(outcome, reasons)

    def _questions(self, point: P, state: DecisionState) -> dict[str, JevQuestion]:
        if point is P.TASK_COMPLETION and state.verification is not None:
            return Q.task_completion()
        if point is P.FAILURE_TRIAGE and state.failure and state.failure.mutations:
            return Q.failure_triage(state.failure.candidates, state.failure.mutations)
        if point is P.TOOL_RISK and state.tool is not None:
            return Q.tool_risk()
        if point is P.PLAN_REVIEW and state.plan is not None:
            return Q.plan_review()
        if point is P.ROUTING and state.routing is not None:
            return Q.route_pick_agent({c: f"Candidate agent {c}" for c in state.routing.candidates})
        return {}
