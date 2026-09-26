"""Property tests for the VerificationReport.overall rule (M4.9, §6, §17.2)."""

from __future__ import annotations

from typing import Literal, get_args

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from aix.domain.enums import CheckKind
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import (
    Check,
    CheckStatus,
    Overall,
    VerificationReport,
    compute_overall,
)

STATUSES = list(get_args(CheckStatus))
RANK: dict[str, int] = {"passed": 0, "warning": 1, "incomplete": 2, "failed": 3}


@st.composite
def checks(draw: st.DrawFn) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=draw(st.sampled_from(list(CheckKind))),
        status=draw(st.sampled_from(STATUSES)),
        required=draw(st.booleans()),
        summary="s",
    )


check_lists = st.lists(checks(), max_size=8)


@given(check_lists, st.randoms())
def test_order_does_not_matter(cs: list[Check], rnd: object) -> None:
    shuffled = list(cs)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert compute_overall(cs) == compute_overall(shuffled)


@given(check_lists, checks())
def test_adding_a_check_never_improves_the_outcome(cs: list[Check], extra: Check) -> None:
    assert RANK[compute_overall([*cs, extra])] >= RANK[compute_overall(cs)]


@given(check_lists, st.sampled_from(["failed", "error"]))
def test_a_required_failure_always_fails_the_report(
    cs: list[Check], status: Literal["failed", "error"]
) -> None:
    bad = Check(
        id=new_id(IdPrefix.CHECK),
        kind=CheckKind.TESTS,
        status=status,
        required=True,
        summary="x",  # type: ignore[arg-type]
    )
    assert compute_overall([*cs, bad]) == "failed"


@given(check_lists)
def test_passed_means_nothing_missing_failed_or_doubtful(cs: list[Check]) -> None:
    if compute_overall(cs) == "passed":
        for c in cs:
            assert c.status in ("passed", "skipped")
            assert not (c.required and c.status == "skipped")


@given(check_lists)
def test_a_required_check_that_did_not_run_is_never_success(cs: list[Check]) -> None:
    if any(c.required and c.status == "skipped" for c in cs):
        assert compute_overall(cs) in ("incomplete", "failed")


@given(check_lists)
def test_optional_checks_can_only_warn(cs: list[Check]) -> None:
    only_optional = [c.model_copy(update={"required": False}) for c in cs]
    assert compute_overall(only_optional) in ("passed", "warning")


@given(check_lists)
def test_an_ai_review_pass_cannot_rescue_a_failed_executed_check(cs: list[Check]) -> None:
    before = compute_overall(cs)
    review = Check(
        id=new_id(IdPrefix.CHECK), kind=CheckKind.AI_REVIEW, status="passed", required=False,
        summary="looks fine",
    )  # fmt: skip
    assert compute_overall([*cs, review]) == before


@given(check_lists, st.sampled_from(list(get_args(Overall))))
def test_report_accepts_exactly_the_computed_overall(cs: list[Check], claimed: Overall) -> None:
    attempt = new_id(IdPrefix.ATTEMPT)
    expected = compute_overall(cs)
    if claimed == expected:
        assert (
            VerificationReport(attempt_id=attempt, checks=cs, overall=claimed).overall == expected
        )
    else:
        with pytest.raises(ValidationError):
            VerificationReport(attempt_id=attempt, checks=cs, overall=claimed)


def test_empty_report_passes() -> None:
    assert compute_overall([]) == "passed"
