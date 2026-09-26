from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check, CheckStatus
from aix.verification.baseline import apply_baseline, run_baseline
from aix.verification.commands import ResolvedCommand

pytestmark = pytest.mark.anyio


def chk(kind: K, status: CheckStatus, **metrics: float) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=kind,
        status=status,
        required=True,
        summary=f"{kind.value} {status}",
        metrics=metrics,
        severity="high" if status == "failed" else "info",
    )


def test_new_failure_without_baseline_is_unchanged() -> None:
    cur = chk(K.TESTS, "failed", tests_failed=2)
    assert apply_baseline(cur, None) == cur
    assert apply_baseline(cur, chk(K.TESTS, "passed")) == cur


def test_passing_check_is_never_touched() -> None:
    cur = chk(K.TESTS, "passed")
    assert apply_baseline(cur, chk(K.TESTS, "failed", tests_failed=3)) == cur


@pytest.mark.parametrize(
    ("kind", "key"),
    [(K.TESTS, "tests_failed"), (K.LINT, "lint_errors"), (K.TYPECHECK, "type_errors")],
)
def test_same_or_fewer_failures_are_pre_existing(kind: K, key: str) -> None:
    for now in (3.0, 2.0):
        out = apply_baseline(chk(kind, "failed", **{key: now}), chk(kind, "failed", **{key: 3.0}))
        assert out.status == "warning" and out.metrics["pre_existing"] == 1.0
        assert out.summary.startswith("pre_existing:") and out.required
        assert out.severity == "info"


@pytest.mark.parametrize(
    ("kind", "key"),
    [(K.TESTS, "tests_failed"), (K.LINT, "lint_errors"), (K.TYPECHECK, "type_errors")],
)
def test_more_failures_than_baseline_still_fail(kind: K, key: str) -> None:
    out = apply_baseline(chk(kind, "failed", **{key: 5.0}), chk(kind, "failed", **{key: 3.0}))
    assert out.status == "failed" and "2 new" in out.summary and "3 pre-existing" in out.summary


def test_check_without_a_count_is_pre_existing_when_it_failed_before() -> None:
    out = apply_baseline(chk(K.BUILD, "failed"), chk(K.BUILD, "failed"))
    assert out.status == "warning" and out.summary.startswith("pre_existing:")


def test_baseline_errors_carry_no_information() -> None:
    cur = chk(K.TESTS, "failed", tests_failed=1)
    assert apply_baseline(cur, chk(K.TESTS, "error")) == cur
    assert apply_baseline(cur, chk(K.TESTS, "skipped")) == cur


def test_current_error_is_not_downgraded() -> None:
    cur = chk(K.TESTS, "error")
    assert apply_baseline(cur, chk(K.TESTS, "failed", tests_failed=1)) == cur


async def test_run_baseline_runs_each_kind_once(tmp_path: Path) -> None:
    py = Path(sys.executable).name
    ok = ResolvedCommand(
        argv=[sys.executable, "-c", "print('ok')"], source="config", available=True
    )
    bad = ResolvedCommand(
        argv=[sys.executable, "-c", "print('Found 2 errors.'); raise SystemExit(1)"],
        source="config", available=True,
    )  # fmt: skip
    base = await run_baseline(
        tmp_path, {K.BUILD: ok, K.LINT: bad}, [K.BUILD, K.LINT, K.TESTS],
        allow=[py], timeout_s=30, out_dir=tmp_path / "b",
    )  # fmt: skip
    assert base.checks[K.BUILD].status == "passed"
    assert (
        base.checks[K.LINT].status == "failed" and base.checks[K.LINT].metrics["lint_errors"] == 2.0
    )
    assert base.checks[K.TESTS].status == "skipped"  # no command for tests
    assert base.get(K.SECRETS) is None
    assert roundtrip(base)


def roundtrip(base) -> bool:  # type: ignore[no-untyped-def]
    from aix.verification.baseline import Baseline

    return Baseline.model_validate_json(base.model_dump_json()) == base
