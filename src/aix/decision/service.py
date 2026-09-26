"""DecisionService (PLAYBOOK §18): gates first, then a provider, always recorded."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence

import anyio
from pydantic import JsonValue

from aix.decision.gates import GateFacts, evaluate_gates
from aix.decision.provider import DecisionProvider, ProviderAnswer
from aix.decision.state import DecisionState
from aix.domain.decisions import DecisionRecord
from aix.domain.enums import DecisionOutcome as O
from aix.domain.enums import DecisionPoint
from aix.domain.ids import IdPrefix, new_id


def canonical_hash(
    state: dict[str, JsonValue], questions: dict[str, JsonValue], policy_version: str
) -> str:
    """SHA-256 of the canonical JSON of ``(state, questions, policy_version)`` (§18.1 rule 4)."""
    blob = json.dumps(
        {"state": state, "questions": questions, "policy": policy_version},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DecisionService:
    """Evaluates gates, consults a provider and records the result.

    ``fallback`` (the deterministic rules provider) decides whenever ``provider`` errors, times
    out, or answers outside the allowed outcomes; the record then carries
    ``<provider>:unavailable`` / ``<provider>:outcome_not_allowed`` / ``<provider>:invalid_choice``.
    """

    def __init__(
        self,
        provider: DecisionProvider,
        fallback: DecisionProvider,
        *,
        policy_version: str,
        timeout_s: float = 3.0,
    ) -> None:
        self._provider = provider
        self._fallback = fallback
        self._policy = policy_version
        self._timeout = timeout_s

    async def _ask(
        self,
        p: DecisionProvider,
        point: DecisionPoint,
        state: DecisionState,
        allowed: set[O],
        candidates: Sequence[str] | None,
        reasons: list[str],
        limit_s: float | None,
    ) -> ProviderAnswer | None:
        try:
            with anyio.fail_after(limit_s):
                ans = await p.decide(point, state, allowed)
        except Exception:
            reasons.append(f"{p.name}:unavailable")
            return None
        if ans.outcome not in allowed:
            reasons.append(f"{p.name}:outcome_not_allowed")
            return None
        if ans.outcome is O.CHOOSE and (candidates is None or ans.choice not in candidates):
            reasons.append(f"{p.name}:invalid_choice")
            return None
        if ans.outcome is not O.CHOOSE and ans.choice is not None:
            ans = ans.model_copy(update={"choice": None})
        reasons.extend(ans.reason_codes)
        return ans

    async def decide(
        self,
        point: DecisionPoint,
        subject: str,
        state: DecisionState,
        facts: GateFacts,
        *,
        candidates: Sequence[str] | None = None,
    ) -> DecisionRecord:
        """Decide ``point`` for ``subject``.

        Contract: the outcome is always within the gates' allowed set; a forced gate outcome is
        returned without consulting any provider (provider recorded as ``rules``); if nothing
        valid comes back, ``ask_human`` (or the first allowed outcome) is used with reason
        ``decision:no_valid_answer``. The record's ``inputs_hash`` covers state, questions and
        policy version.
        """
        gate = evaluate_gates(point, facts)
        allowed = set(gate.allowed_outcomes)
        reasons = list(gate.reason_codes)
        wire = state.to_wire()
        answer: ProviderAnswer | None = None
        provider_name = self._fallback.name
        if gate.forced_outcome is not None:
            answer = ProviderAnswer(outcome=gate.forced_outcome)
            provider_name = "rules"
        else:
            answer = await self._ask(
                self._provider, point, state, allowed, candidates, reasons, self._timeout
            )
            provider_name = self._provider.name
            if answer is None:
                answer = await self._ask(
                    self._fallback, point, state, allowed, candidates, reasons, None
                )
                provider_name = self._fallback.name
        if answer is None:
            outcome = O.ASK_HUMAN if O.ASK_HUMAN in allowed else gate.allowed_outcomes[0]
            answer = ProviderAnswer(outcome=outcome)
            reasons.append("decision:no_valid_answer")
            provider_name = "rules"
        return DecisionRecord(
            id=new_id(IdPrefix.DECISION),
            point=point,
            subject=subject,
            provider=provider_name,
            gate_result=gate,
            state=wire,
            questions=answer.questions,
            answers=answer.answers,
            outcome=answer.outcome,
            choice=answer.choice,
            reason_codes=reasons,
            provider_meta=answer.meta,
            inputs_hash=canonical_hash(wire, answer.questions, self._policy),
        )
