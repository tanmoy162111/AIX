"""Jev question catalog (PLAYBOOK Appendix A). Instructions are literal and narrow.

Each builder returns ``{name: JevQuestion}``; ``mapping.py`` turns the answers into outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aix.decision.jev import JevQuestion, choice_q, noul_q, score_q

RISK_LEVELS = (
    "No remaining findings",
    "Only low findings",
    "Medium findings remain",
    "High or critical findings remain",
)
TOOL_RISK_LEVELS = (
    "Read-only",
    "Local reversible write",
    "Local irreversible or wide write",
    "External side effect",
)
PLAN_RISK_LEVELS = ("Low plan risk", "Medium plan risk", "High plan risk")


def task_completion() -> dict[str, JevQuestion]:
    """A.1: what should happen to this attempt."""
    return {
        "completion": choice_q(
            "Given only the verification facts, what should happen to this attempt",
            {
                "accept": "All required checks passed and no unresolved medium or higher findings",
                "fix_and_retry": "A specific check or finding failed that the same agent can "
                "address",
                "different_agent": "Repeated failures of the same kind suggest a different agent "
                "should try",
                "needs_human": "Facts are contradictory, incomplete, or the risk is high",
            },
        ),
        "residual_risk": score_q(
            "How much risk remains in the verification facts", list(RISK_LEVELS)
        ),
        "warnings_blocking": noul_q(
            "At least one warning in the verification facts should block acceptance for a task of "
            "this risk level"
        ),
    }


def failure_triage(candidates: Sequence[str], mutations: Sequence[str]) -> dict[str, JevQuestion]:
    """A.2: refine the failure class among rule-produced candidates and pick a retry mutation.

    A choice needs two options, so ``failure_class`` is omitted when the rules produced a single
    candidate. Callers must not ask at all with fewer than two mutations (nothing to choose).
    """
    questions: dict[str, JevQuestion] = {}
    if len(candidates) >= 2:
        questions["failure_class"] = choice_q(
            "Which failure class best matches the failure facts",
            {c: f"The failure facts match the class {c}" for c in candidates},
        )
    questions["mutation"] = choice_q(
        "Which retry approach is most likely to succeed",
        {m: f"Retry using the approach {m}" for m in mutations},
    )
    questions["likely_transient"] = noul_q(
        "The failure facts indicate a transient environmental problem rather than a defect "
        "in the change"
    )
    return questions


def tool_risk() -> dict[str, JevQuestion]:
    """A.3: how risky a control-plane executed command is."""
    return {
        "risk": score_q("How risky is this action", list(TOOL_RISK_LEVELS)),
        "outside_scope": noul_q("The target paths are outside the task scope"),
    }


def route_pick_agent(candidates: Mapping[str, str]) -> dict[str, JevQuestion]:
    """A.4: tie-break among agents; ``candidates`` maps id -> a control-plane stats line."""
    return {"agent": choice_q("Which agent is the best fit for this task type", dict(candidates))}


def plan_review() -> dict[str, JevQuestion]:
    """A.5: sanity review of a high-risk plan."""
    return {
        "external_side_effects": noul_q("The plan includes a step with external side effects"),
        "missing_verification": noul_q("The plan omits a verification step for a write task"),
        "plan_risk": score_q("How risky is this plan", list(PLAN_RISK_LEVELS)),
    }


def intent_classify() -> dict[str, JevQuestion]:
    """A.6: classify a user goal. The goal text is the user's own input (self-directed)."""
    return {
        "kind": choice_q(
            "What kind of work is this goal",
            {
                "coding": "Change or add code",
                "research": "Investigate or explain",
                "review": "Review existing changes",
                "security": "Security audit or fix",
                "devops": "Build, deploy or infrastructure",
                "docs": "Write documentation",
                "other": "None of the above",
            },
        ),
        "risk": choice_q(
            "How risky is this goal",
            {
                "low": "Local, reversible, no sensitive systems",
                "medium": "Touches authentication, payments, configuration or dependencies",
                "high": "Deploys, migrates data, deletes, or handles credentials",
            },
        ),
    }
