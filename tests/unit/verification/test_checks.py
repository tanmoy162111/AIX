from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aix.domain.enums import CheckKind as K
from aix.domain.verification import compute_overall
from aix.verification.checks import CheckResult, run_command_check
from aix.verification.commands import ResolvedCommand

pytestmark = pytest.mark.anyio
PY = Path(sys.executable).name


def cmd(code: str, *, available: bool = True) -> ResolvedCommand:
    return ResolvedCommand(argv=[sys.executable, "-c", code], source="config", available=available)


async def check(
    tmp: Path,
    kind: K,
    command: ResolvedCommand | None,
    *,
    required: bool = True,
    timeout_s: float = 30,
) -> CheckResult:
    return await run_command_check(
        kind,
        command,
        tmp,
        required=required,
        allow=[PY, "pytest"],
        timeout_s=timeout_s,
        out_dir=tmp / "out",
    )


async def test_passing_and_failing_build(tmp_path: Path) -> None:
    ok = await check(tmp_path, K.BUILD, cmd("print('built')"))
    assert ok.check.status == "passed" and ok.check.required and ok.check.kind is K.BUILD
    assert ok.check.command and ok.check.command[-1] == "print('built')"
    bad = await check(tmp_path, K.BUILD, cmd("import sys; sys.exit(2)"))
    assert bad.check.status == "failed" and "exit code 2" in bad.check.summary
    assert bad.check.severity == "high"


async def test_missing_command_or_tool_is_skipped_not_passed(tmp_path: Path) -> None:
    none = await check(tmp_path, K.LINT, None)
    assert none.check.status == "skipped" and "no command" in none.check.summary
    gone = await check(tmp_path, K.LINT, cmd("pass", available=False))
    assert gone.check.status == "skipped" and "tool not available" in gone.check.summary
    assert compute_overall([gone.check]) == "incomplete"


async def test_optional_check_failure_is_a_warning_overall(tmp_path: Path) -> None:
    r = await check(tmp_path, K.LINT, cmd("import sys; sys.exit(1)"), required=False)
    assert r.check.status == "failed" and not r.check.required
    assert compute_overall([r.check]) == "warning"


async def test_timeout_and_blocked_are_errors(tmp_path: Path) -> None:
    slow = await check(tmp_path, K.TESTS, cmd("import time; time.sleep(30)"), timeout_s=1)
    assert slow.check.status == "error" and "timed out" in slow.check.summary
    blocked = await run_command_check(
        K.BUILD, cmd("pass"), tmp_path, required=True, allow=["git"], timeout_s=5
    )
    assert blocked.check.status == "error" and "allowlist" in blocked.check.summary


async def test_output_files_are_kept(tmp_path: Path) -> None:
    r = await check(tmp_path, K.BUILD, cmd("print('hello-out')"))
    assert r.outcome is not None and r.outcome.stdout_path is not None
    assert "hello-out" in r.outcome.stdout_path.read_text()


async def test_lint_and_typecheck_metrics_from_summaries(tmp_path: Path) -> None:
    lint = await check(tmp_path, K.LINT, cmd("print('Found 3 errors.'); raise SystemExit(1)"))
    assert lint.check.status == "failed" and lint.check.metrics == {"lint_errors": 3.0}
    typ = await check(
        tmp_path,
        K.TYPECHECK,
        cmd("print('12 errors, 0 warnings, 0 informations'); raise SystemExit(1)"),
    )
    assert typ.check.metrics == {"type_errors": 12.0}
    clean = await check(tmp_path, K.LINT, cmd("print('All checks passed!')"))
    assert clean.check.status == "passed" and clean.check.metrics == {"lint_errors": 0.0}


async def test_real_pytest_run_parses_junit_metrics(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text(
        "def test_ok():\n    assert True\n\ndef test_bad():\n    assert 1 == 2\n\n"
        "import pytest\n@pytest.mark.skip\ndef test_skip():\n    pass\n"
    )
    pytest_cmd = ResolvedCommand(
        argv=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        source="detected",
        available=True,
    )
    r = await check(tmp_path, K.TESTS, pytest_cmd)
    assert r.check.status == "failed"
    assert r.check.metrics == {
        "tests_total": 3.0, "tests_passed": 1.0, "tests_failed": 1.0, "tests_skipped": 1.0,
    }  # fmt: skip
    assert "1 of 3 tests failed" in r.check.summary and "test_a::test_bad" in r.check.summary


async def test_pytest_with_no_tests_is_a_warning(tmp_path: Path) -> None:
    pytest_cmd = ResolvedCommand(
        argv=[sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        source="detected",
        available=True,
    )
    r = await check(tmp_path, K.TESTS, pytest_cmd)
    assert r.check.status == "warning" and "no tests" in r.check.summary
