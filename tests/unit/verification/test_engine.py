from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

import repos
from aix.config.schema import AixConfig, SecurityChecksConfig, SecurityConfig, VerificationConfig
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import VerificationSpec
from aix.domain.verification import Check, VerificationReport
from aix.verification.baseline import Baseline, run_baseline
from aix.verification.commands import ResolvedCommand
from aix.verification.engine import run_verification

pytestmark = pytest.mark.anyio
PY = Path(sys.executable).name
PLANTED = (repos.PATCHES / "planted_secret.diff").read_text()


def py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


OK = py("print('fine')")
BAD_LINT = py("print('Found 2 errors.'); raise SystemExit(1)")


def config(commands: dict[K, list[str]], **sec: str) -> AixConfig:
    return AixConfig(
        verification=VerificationConfig(
            commands=commands,
            security=SecurityChecksConfig(**sec),  # type: ignore[arg-type]
        ),
        security=SecurityConfig(shell_allow=[PY]),
    )


async def verify(
    tmp: Path,
    cfg: AixConfig,
    *,
    required: list[K],
    optional: list[K] | None = None,
    patch: str = "",
    paths: list[str] | None = None,
    scope: list[str] | None = None,
    baseline: Baseline | None = None,
    reviewer=None,  # type: ignore[no-untyped-def]
) -> VerificationReport:
    return await run_verification(
        tmp, new_id(IdPrefix.ATTEMPT), VerificationSpec(required=required, optional=optional or []),
        cfg, patch=patch, changed_paths=paths or [], file_scope=scope or ["**"],
        baseline=baseline, out_dir=tmp / "out", reviewer=reviewer, goal="goal",
    )  # fmt: skip


def by_kind(r: VerificationReport) -> dict[K, Check]:
    return {c.kind: c for c in r.checks}


async def test_everything_green(tmp_path: Path) -> None:
    cfg = config({K.BUILD: OK, K.TESTS: OK, K.LINT: OK})
    r = await verify(tmp_path, cfg, required=[K.BUILD, K.TESTS, K.LINT])
    assert r.overall == "passed"
    kinds = by_kind(r)
    assert set(kinds) == {K.BUILD, K.TESTS, K.LINT, K.POLICY, K.SECRETS}
    assert all(kinds[k].required for k in (K.BUILD, K.TESTS, K.LINT, K.POLICY, K.SECRETS))


async def test_failing_required_check_fails_the_report(tmp_path: Path) -> None:
    cfg = config({K.BUILD: OK, K.TESTS: py("raise SystemExit(1)")})
    r = await verify(tmp_path, cfg, required=[K.BUILD, K.TESTS])
    assert r.overall == "failed" and by_kind(r)[K.TESTS].status == "failed"


async def test_required_check_without_a_command_makes_the_report_incomplete(tmp_path: Path) -> None:
    r = await verify(tmp_path, config({K.BUILD: OK}), required=[K.BUILD, K.LINT])
    assert by_kind(r)[K.LINT].status == "skipped" and r.overall == "incomplete"


async def test_unavailable_tool_is_skipped_so_incomplete(tmp_path: Path) -> None:
    cfg = config({K.BUILD: OK, K.LINT: ["no-such-linter-xyz"]})
    r = await verify(tmp_path, cfg, required=[K.BUILD, K.LINT])
    assert by_kind(r)[K.LINT].status == "skipped" and r.overall == "incomplete"


async def test_optional_check_failure_only_warns(tmp_path: Path) -> None:
    cfg = config({K.BUILD: OK, K.TYPECHECK: py("raise SystemExit(1)")})
    r = await verify(tmp_path, cfg, required=[K.BUILD], optional=[K.TYPECHECK])
    assert not by_kind(r)[K.TYPECHECK].required and r.overall == "warning"


async def test_baseline_turns_an_existing_failure_into_a_warning(tmp_path: Path) -> None:
    cfg = config({K.BUILD: OK, K.LINT: BAD_LINT})
    cmds = {K.LINT: ResolvedCommand(argv=BAD_LINT, source="config", available=True)}
    base = await run_baseline(tmp_path, cmds, [K.LINT], allow=[PY], timeout_s=30)
    r = await verify(tmp_path, cfg, required=[K.BUILD, K.LINT], baseline=base)
    lint = by_kind(r)[K.LINT]
    assert lint.status == "warning" and lint.summary.startswith("pre_existing:")
    assert r.overall == "warning"
    worse = config({K.BUILD: OK, K.LINT: py("print('Found 5 errors.'); raise SystemExit(1)")})
    assert (await verify(tmp_path, worse, required=[K.LINT], baseline=base)).overall == "failed"


async def test_planted_secret_fails_without_leaking_it(tmp_path: Path) -> None:
    r = await verify(tmp_path, config({K.BUILD: OK}), required=[K.BUILD], patch=PLANTED)
    secrets = by_kind(r)[K.SECRETS]
    assert r.overall == "failed" and secrets.status == "failed" and secrets.severity == "critical"
    assert "AKIAIOSFODNN7EXAMPLE" not in r.model_dump_json()


async def test_policy_violation_fails(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("x")
    r = await verify(
        tmp_path, config({K.BUILD: OK}), required=[K.BUILD], paths=["docs/x.md"], scope=["src/**"]
    )
    assert by_kind(r)[K.POLICY].status == "failed" and r.overall == "failed"


async def test_security_modes(tmp_path: Path) -> None:
    off = config({K.BUILD: OK}, secrets="off", sast="off", deps="off")
    r = await verify(tmp_path, off, required=[K.BUILD], patch=PLANTED)
    assert K.SECRETS not in by_kind(r) and r.overall == "passed"
    # sast/deps in "auto" run only when the task's spec lists them; with no tools they are skipped
    auto = config({K.BUILD: OK})
    plain = await verify(tmp_path, auto, required=[K.BUILD])
    assert K.SECURITY_SAST not in by_kind(plain)
    listed = await verify(tmp_path, auto, required=[K.BUILD], optional=[K.SECURITY_SAST, K.DEPS])
    assert by_kind(listed)[K.SECURITY_SAST].status == "skipped"
    assert listed.overall == "passed"  # optional + skipped does not spoil the report


async def test_ai_review_is_added_when_a_reviewer_is_given(tmp_path: Path) -> None:
    seen: list[Sequence[Check]] = []

    async def reviewer(diff: str, checks: Sequence[Check]) -> Check:
        seen.append(checks)
        return Check(id=new_id(IdPrefix.CHECK), kind=K.AI_REVIEW, status="failed", required=False,
                     severity="high", summary="reviewed by r: 1 finding(s)")  # fmt: skip

    cfg = config({K.BUILD: OK})
    r = await verify(
        tmp_path, cfg, required=[K.BUILD], optional=[K.AI_REVIEW], patch="+x\n", reviewer=reviewer
    )
    assert by_kind(r)[K.AI_REVIEW].status == "failed" and r.overall == "warning"
    assert {c.kind for c in seen[0]} >= {K.BUILD, K.POLICY}  # the reviewer sees executed facts
    none = await verify(tmp_path, cfg, required=[K.BUILD], optional=[K.AI_REVIEW])
    assert K.AI_REVIEW not in by_kind(none)  # listed but nobody available: not silently passed


async def test_checks_have_unique_ids_and_report_is_valid(tmp_path: Path) -> None:
    r = await verify(tmp_path, config({K.BUILD: OK}), required=[K.BUILD])
    assert len({c.id for c in r.checks}) == len(r.checks)
    assert VerificationReport.model_validate_json(r.model_dump_json()) == r
