"""Jev answers -> decision outcomes (PLAYBOOK §18.5, Appendix A).

Each mapper returns ``(outcome, reason_codes)``, or ``None`` when the rules provider should
decide instead; in that case the reason says why (``jev:low_confidence`` ...). Jev can never
pick an outcome the gates forbid: the Decision Service checks membership afterwards.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aix.decision.jev import JevAnswer
from aix.decision.state import DecisionState
from aix.decision.thresholds import Thresholds
from aix.domain.enums import DecisionOutcome as O

Mapped = tuple[O, list[str]] | tuple[None, list[str]]

MUTATION_OUTCOME: dict[str, O] = {
    "same_agent_with_failure_context": O.RETRY,
    "same_agent_with_findings": O.RETRY,
    "same_agent_clarified_prompt": O.RETRY,
    "same_agent_with_scope_reminder": O.RETRY,
    "same_agent_new_context": O.RETRY,
    "add_research_step": O.RETRY,
    "split_task": O.RETRY,
    "wait_and_retry": O.RETRY,
    "rebase_and_retry": O.RETRY,
    "compact_context_and_retry": O.RETRY,
    "more_verification": O.RETRY,
    "switch_agent": O.SWITCH_AGENT,
    "ask_human": O.ASK_HUMAN,
}


def _get(a: Mapping[str, JevAnswer], name: str) -> JevAnswer | None:
    return a.get(name)


def map_task_completion(
    answers: Mapping[str, JevAnswer], state: DecisionState, th: Thresholds
) -> Mapped:
    """§18.5: accept only with high confidence, no blocking warning and low residual risk."""
    completion = _get(answers, "completion")
    risk = _get(answers, "residual_risk")
    blocking = _get(answers, "warnings_blocking")
    if completion is None or completion.choice is None or completion.confidence is None:
        return None, ["jev:incomplete_answer"]
    task_risk = state.task.risk if state.task else None
    conf = completion.confidence
    if (
        completion.choice == "accept"
        and conf >= th.get("task_completion.accept_confidence", task_risk)
        and blocking is not None
        and (blocking.noul or 0.0) < th.get("task_completion.max_blocking_noul")
        and risk is not None
        and (risk.score if risk.score is not None else 99.0)
        < th.get("task_completion.max_residual_risk")
    ):
        return O.ACCEPT, ["jev:accept"]
    if conf < th.get("task_completion.low_confidence"):
        return None, ["jev:low_confidence"]
    return {
        "fix_and_retry": (O.RETRY, ["jev:fix_and_retry"]),
        "different_agent": (O.SWITCH_AGENT, ["jev:different_agent"]),
        "needs_human": (O.ASK_HUMAN, ["jev:needs_human"]),
    }.get(completion.choice, (None, ["jev:accept_not_confident"]))


def map_failure_triage(
    answers: Mapping[str, JevAnswer],
    state: DecisionState,
    th: Thresholds,
    allowed_mutations: Sequence[str],
) -> tuple[O | None, list[str], str | None, str | None]:
    """Returns ``(outcome, reasons, mutation, refined_class)``; refinement stays in candidates."""
    mut = _get(answers, "mutation")
    cls = _get(answers, "failure_class")
    refined = None
    if cls and cls.choice and state.failure and cls.choice in state.failure.candidates:
        refined = cls.choice
    if mut is None or mut.choice is None or mut.confidence is None:
        return None, ["jev:incomplete_answer"], None, refined
    if mut.confidence < th.get("failure_triage.min_confidence"):
        return None, ["jev:low_confidence"], None, refined
    if mut.choice not in allowed_mutations or mut.choice not in MUTATION_OUTCOME:
        return None, ["jev:mutation_not_in_sequence"], None, refined
    return MUTATION_OUTCOME[mut.choice], [f"jev:mutation:{mut.choice}"], mut.choice, refined


def map_tool_risk(answers: Mapping[str, JevAnswer], th: Thresholds) -> Mapped:
    """A.3: outside scope or an external side effect denies; irreversible writes ask a human."""
    risk, outside = _get(answers, "risk"), _get(answers, "outside_scope")
    if risk is None or risk.score is None or outside is None or outside.noul is None:
        return None, ["jev:incomplete_answer"]
    if outside.noul > th.get("tool_risk.deny_if_p_risky_above"):
        return O.DENY, ["jev:outside_scope"]
    if risk.score >= th.get("tool_risk.deny_risk_score"):
        return O.DENY, ["jev:external_side_effect"]
    if risk.score >= th.get("tool_risk.ask_risk_score"):
        return O.ASK_HUMAN, ["jev:irreversible_write"]
    return O.ALLOW, ["jev:low_risk"]


def map_plan_review(answers: Mapping[str, JevAnswer], th: Thresholds) -> Mapped:
    """A.5: side effects ask a human, a missing verification step rejects, high risk asks."""
    ext = _get(answers, "external_side_effects")
    missing = _get(answers, "missing_verification")
    risk = _get(answers, "plan_risk")
    if None in (ext, missing, risk) or ext is None or missing is None or risk is None:
        return None, ["jev:incomplete_answer"]
    if (ext.noul or 0.0) > th.get("plan_review.side_effect_noul"):
        return O.ASK_HUMAN, ["jev:external_side_effects"]
    if (missing.noul or 0.0) > th.get("plan_review.missing_verification_noul"):
        return O.REJECT, ["jev:missing_verification"]
    if (risk.score or 0.0) >= th.get("plan_review.ask_risk_score"):
        return O.ASK_HUMAN, ["jev:high_plan_risk"]
    return O.ACCEPT, ["jev:plan_ok"]


def map_routing(answers: Mapping[str, JevAnswer], th: Thresholds) -> tuple[str | None, list[str]]:
    """A.4: the chosen candidate id if Jev is confident enough."""
    agent = _get(answers, "agent")
    if agent is None or agent.choice is None or agent.confidence is None:
        return None, ["jev:incomplete_answer"]
    if agent.confidence < th.get("routing.min_confidence"):
        return None, ["jev:low_confidence"]
    return agent.choice, ["jev:route"]
