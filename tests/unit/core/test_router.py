from __future__ import annotations

import math
from collections.abc import Callable

import pytest

from aix.config.schema import RoutingConfig
from aix.core.router.rules import AgentStat, RoutingContext, bayesian_success_rate, route
from aix.domain.agents import AgentSpec
from aix.domain.enums import Capability, FailureClass, TaskType
from aix.domain.errors import NoEligibleAgent
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Task

C = Capability


def agent(
    agent_id: str,
    caps: dict[Capability, float] | None = None,
    *,
    health: str = "ready",
    cost: str = "medium",
) -> AgentSpec:
    return AgentSpec(
        id=agent_id,
        name=agent_id,
        kind="local",
        capabilities=caps if caps is not None else {C.IMPLEMENT: 0.8, C.REVIEW: 0.8, C.TEST: 0.8},
        health=health,  # type: ignore[arg-type]
        cost_class=cost,  # type: ignore[arg-type]
    )


def task(type_: TaskType = TaskType.IMPLEMENT, caps: list[Capability] | None = None) -> Task:
    return Task(
        id=new_id(IdPrefix.TASK),
        run_id=new_id(IdPrefix.RUN),
        title="t",
        goal="g",
        type=type_,
        required_capabilities=[C.IMPLEMENT] if caps is None else caps,
    )


def ctx(agents: list[AgentSpec], t: Task | None = None, **kw: object) -> RoutingContext:
    return RoutingContext(task=t or task(), agents=agents, **kw)  # type: ignore[arg-type]


# ---- scoring ------------------------------------------------------------------------------


def test_bayesian_rate_uses_a_beta_2_2_prior() -> None:
    assert bayesian_success_rate(0, 0) == 0.5
    assert bayesian_success_rate(8, 8) == pytest.approx(10 / 12)
    assert bayesian_success_rate(0, 8) == pytest.approx(2 / 12)


def test_score_formula_matches_the_spec() -> None:
    a = agent("a", {C.IMPLEMENT: 0.8}, cost="free")
    d = route(ctx([a], stats={("a", TaskType.IMPLEMENT): AgentStat(attempts=8, accepted=6)}))
    observed = 8 / 12
    expected = 0.45 * observed + 0.35 * 0.8 + 0.10 * 1.0 + 0.10 * 0.5
    assert d.scores["a"] == pytest.approx(expected, abs=1e-3)
    assert d.primary == "a" and d.fallbacks == []


def test_prior_is_the_mean_over_required_capabilities() -> None:
    a = agent("a", {C.IMPLEMENT: 0.9, C.TEST: 0.5})
    b = agent("b", {C.IMPLEMENT: 0.7, C.TEST: 0.7})
    d = route(ctx([a, b], task(caps=[C.IMPLEMENT, C.TEST])))
    assert d.scores["a"] == pytest.approx(d.scores["b"])  # both mean 0.7
    assert d.primary == "a"  # tie broken by id


def test_history_beats_a_slightly_better_prior() -> None:
    a = agent("a", {C.IMPLEMENT: 0.9})
    b = agent("b", {C.IMPLEMENT: 0.8})
    stats = {
        ("a", TaskType.IMPLEMENT): AgentStat(attempts=20, accepted=2),
        ("b", TaskType.IMPLEMENT): AgentStat(attempts=20, accepted=18),
    }
    assert route(ctx([a, b], stats=stats)).primary == "b"


def test_cheaper_and_faster_agents_score_higher_all_else_equal() -> None:
    cheap, dear = agent("cheap", cost="free"), agent("dear", cost="high")
    assert route(ctx([dear, cheap])).primary == "cheap"
    fast = agent("fast")
    slow = agent("slow")
    stats = {
        ("fast", TaskType.IMPLEMENT): AgentStat(p50_latency_s=10),
        ("slow", TaskType.IMPLEMENT): AgentStat(p50_latency_s=1200),
    }
    assert route(ctx([slow, fast], stats=stats)).primary == "fast"


def test_primary_and_two_fallbacks_ordered_by_score() -> None:
    agents = [
        agent(n, {C.IMPLEMENT: p}) for n, p in [("a", 0.5), ("b", 0.9), ("c", 0.7), ("d", 0.6)]
    ]
    d = route(ctx(agents))
    assert (d.primary, d.fallbacks) == ("b", ["c", "d"])


def test_routing_is_deterministic() -> None:
    agents = [agent(n) for n in "cab"]
    assert route(ctx(agents)) == route(ctx(list(reversed(agents))))


# ---- eligibility --------------------------------------------------------------------------


@pytest.mark.parametrize("health", ["unavailable", "disabled"])
def test_unhealthy_agents_are_ineligible(health: str) -> None:
    d = route(ctx([agent("down", health=health), agent("up")]))
    assert d.primary == "up" and "down" not in d.scores


def test_missing_capability_is_ineligible() -> None:
    d = route(ctx([agent("nocap", {C.REVIEW: 0.9}), agent("ok", {C.IMPLEMENT: 0.3})]))
    assert d.primary == "ok"


def test_zero_prior_means_unsupported() -> None:
    with pytest.raises(NoEligibleAgent):
        route(ctx([agent("zero", {C.IMPLEMENT: 0.0})]))


def test_degraded_only_used_when_no_ready_agent() -> None:
    d = route(
        ctx(
            [
                agent("deg", {C.IMPLEMENT: 0.99}, health="degraded"),
                agent("ready", {C.IMPLEMENT: 0.2}),
            ]
        )
    )
    assert d.primary == "ready" and "deg" not in d.scores
    d = route(ctx([agent("deg", health="degraded")]))
    assert d.primary == "deg" and "degraded_only" in d.reason_codes


def test_policy_can_forbid_an_agent() -> None:
    allow: Callable[[AgentSpec, Task], bool] = lambda a, _t: a.id != "blocked"  # noqa: E731
    d = route(
        ctx(
            [agent("blocked", {C.IMPLEMENT: 0.99}), agent("other", {C.IMPLEMENT: 0.2})],
            policy_allows=allow,
        )
    )
    assert d.primary == "other"


# ---- penalties ----------------------------------------------------------------------------


def test_agent_that_already_failed_the_task_is_penalised() -> None:
    a, b = agent("a", {C.IMPLEMENT: 0.9}), agent("b", {C.IMPLEMENT: 0.7})
    plain = route(ctx([a, b]))
    d = route(ctx([a, b], failed_agents=frozenset({"a"})))
    assert plain.primary == "a" and d.primary == "b"
    assert d.scores["a"] == pytest.approx(plain.scores["a"] - 0.5, abs=1e-3)
    assert "penalty:failed_before:a" in d.reason_codes


def test_same_agent_new_context_mutation_lifts_the_failure_penalty() -> None:
    a, b = agent("a", {C.IMPLEMENT: 0.9}), agent("b", {C.IMPLEMENT: 0.7})
    d = route(ctx([a, b], failed_agents=frozenset({"a"}), same_agent_new_context=True))
    assert d.primary == "a"


def test_reviewer_independence_penalty_applies_to_review_tasks_only() -> None:
    author = agent("author", {C.REVIEW: 0.95, C.IMPLEMENT: 0.95})
    other = agent("other", {C.REVIEW: 0.7, C.IMPLEMENT: 0.7})
    review = task(TaskType.REVIEW, [C.REVIEW])
    d = route(ctx([author, other], review, authored_by="author"))
    assert d.primary == "other" and "penalty:not_independent:author" in d.reason_codes
    impl = route(ctx([author, other], authored_by="author"))
    assert impl.primary == "author"


def test_independence_penalty_can_be_disabled_and_covers_security_review() -> None:
    author = agent("author", {C.SECURITY: 0.95})
    other = agent("other", {C.SECURITY: 0.7})
    t = task(TaskType.SECURITY_REVIEW, [C.SECURITY])
    off = RoutingConfig(prefer_independent_reviewer=False)
    assert route(ctx([author, other], t, authored_by="author", config=off)).primary == "author"
    assert route(ctx([author, other], t, authored_by="author")).primary == "other"


def test_sole_agent_may_review_its_own_change_despite_the_penalty() -> None:
    only = agent("only", {C.REVIEW: 0.9})
    d = route(ctx([only], task(TaskType.REVIEW, [C.REVIEW]), authored_by="only"))
    assert d.primary == "only" and d.scores["only"] < 0


def test_over_budget_agents_are_excluded() -> None:
    a, b = agent("a", {C.IMPLEMENT: 0.9}), agent("b", {C.IMPLEMENT: 0.5})
    d = route(ctx([a, b], budget_remaining_usd=1.0, expected_cost_usd={"a": 5.0, "b": 0.5}))
    assert d.primary == "b" and "over_budget:a" in d.reason_codes
    assert "a" not in d.scores
    with pytest.raises(NoEligibleAgent) as exc:
        route(ctx([a], budget_remaining_usd=1.0, expected_cost_usd={"a": 5.0}))
    assert "exceeds remaining budget" in str(exc.value)
    assert "over_budget:a" in str(exc.value.details["reasons"])


# ---- static pins --------------------------------------------------------------------------


def test_static_pin_overrides_scoring_but_keeps_fallbacks() -> None:
    a, b, c = (
        agent("a", {C.IMPLEMENT: 0.9}),
        agent("b", {C.IMPLEMENT: 0.3}),
        agent("c", {C.IMPLEMENT: 0.6}),
    )
    cfg = RoutingConfig(static={"implement": "b"})
    d = route(ctx([a, b, c], config=cfg))
    assert d.primary == "b" and d.fallbacks == ["a", "c"]
    assert "static_pin:b" in d.reason_codes


@pytest.mark.parametrize("kind", ["unavailable", "missing_capability", "policy"])
def test_static_pin_must_still_be_eligible(kind: str) -> None:
    good = agent("good", {C.IMPLEMENT: 0.6})
    pinned = {
        "unavailable": agent("pin", {C.IMPLEMENT: 0.9}, health="unavailable"),
        "missing_capability": agent("pin", {C.REVIEW: 0.9}),
        "policy": agent("pin", {C.IMPLEMENT: 0.9}),
    }[kind]
    allow: Callable[[AgentSpec, Task], bool] = (
        (lambda a, _t: a.id != "pin") if kind == "policy" else (lambda _a, _t: True)
    )
    d = route(
        ctx([good, pinned], config=RoutingConfig(static={"implement": "pin"}), policy_allows=allow)
    )
    assert d.primary == "good" and "static_pin_ineligible:pin" in d.reason_codes


def test_static_pin_for_another_task_type_is_ignored() -> None:
    a, b = agent("a", {C.IMPLEMENT: 0.9}), agent("b", {C.IMPLEMENT: 0.3})
    d = route(ctx([a, b], config=RoutingConfig(static={"review": "b"})))
    assert d.primary == "a"


# ---- no eligible agent --------------------------------------------------------------------


@pytest.mark.parametrize(
    "agents",
    [
        [],
        [agent("down", health="unavailable")],
        [agent("wrong", {C.DESIGN: 0.9})],
    ],
)
def test_no_eligible_agent(agents: list[AgentSpec]) -> None:
    with pytest.raises(NoEligibleAgent) as exc:
        route(ctx(agents))
    assert exc.value.failure_class is FailureClass.NO_ELIGIBLE_AGENT


def test_scores_are_finite_and_rounded() -> None:
    d = route(ctx([agent("a"), agent("b")]))
    assert all(math.isfinite(v) and round(v, 4) == v for v in d.scores.values())
