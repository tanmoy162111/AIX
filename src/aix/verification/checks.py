"""Command-based checks: build, tests, lint, typecheck (PLAYBOOK §17.2, §17.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import anyio

from aix.domain.enums import CheckKind
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check, CheckStatus, Severity
from aix.verification.commands import ResolvedCommand
from aix.verification.parsers import ParseError, parse_junit
from aix.verification.runner import CommandOutcome, run_command

_SEVERITY: dict[CheckKind, Severity] = {
    CheckKind.BUILD: "high",
    CheckKind.TESTS: "high",
    CheckKind.TYPECHECK: "medium",
    CheckKind.LINT: "low",
}
_LINT_COUNT = re.compile(r"Found (\d+) errors?", re.IGNORECASE)
_TYPE_COUNT = re.compile(r"(\d+) errors?\b", re.IGNORECASE)
PYTEST_NO_TESTS = 5


@dataclass(frozen=True)
class CheckResult:
    """A finished check plus the raw process outcome (output files) when a process ran."""

    check: Check
    outcome: CommandOutcome | None = None


def _check(
    kind: CheckKind,
    status: CheckStatus,
    summary: str,
    *,
    required: bool,
    argv: list[str] | None = None,
    metrics: dict[str, float] | None = None,
    duration_ms: int = 0,
) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=kind,
        status=status,
        severity=_SEVERITY.get(kind, "info") if status in ("failed", "error") else "info",
        required=required,
        summary=summary,
        metrics=metrics or {},
        command=argv,
        duration_ms=duration_ms,
    )


def _is_pytest(argv: list[str]) -> bool:
    return Path(argv[0]).name == "pytest" or argv[1:3] == ["-m", "pytest"]


async def run_command_check(
    kind: CheckKind,
    command: ResolvedCommand | None,
    cwd: Path,
    *,
    required: bool,
    allow: list[str],
    timeout_s: float,
    network: str = "deny",
    out_dir: Path | None = None,
) -> CheckResult:
    """Run one build/tests/lint/typecheck check and turn the result into a :class:`Check`.

    Contract: never raises for command problems and never reports ``passed`` for something that
    did not run. No command or a missing tool -> ``skipped`` (a required skipped check makes the
    report ``incomplete``, §17.2); blocked or timed-out -> ``error``; non-zero exit -> ``failed``.
    For tests, ``pytest`` gets ``--junitxml`` and the counts become metrics.
    """
    if command is None:
        return CheckResult(
            _check(kind, "skipped", f"no command detected for {kind.value}", required=required)
        )
    argv = list(command.argv)
    if not command.available:
        return CheckResult(
            _check(kind, "skipped", f"tool not available: {argv[0]}", required=required, argv=argv)
        )
    junit: Path | None = None
    if kind is CheckKind.TESTS and _is_pytest(argv) and out_dir is not None:
        junit = out_dir / "junit.xml"
        argv += [f"--junitxml={junit}"]
    out = await run_command(
        argv,
        cwd,
        allow=allow,
        timeout_s=timeout_s,
        network=network,
        out_dir=out_dir,
        name=kind.value,
    )
    common = {"required": required, "argv": argv, "duration_ms": out.duration_ms}

    def result(
        status: CheckStatus, summary: str, metrics: dict[str, float] | None = None
    ) -> CheckResult:
        return CheckResult(_check(kind, status, summary, metrics=metrics, **common), out)  # type: ignore[arg-type]

    if out.status == "blocked":
        return result("error", f"blocked by policy: {out.detail}")
    if out.status == "tool_missing":
        return result("skipped", f"tool not available: {out.detail}")
    if out.status == "timeout":
        return result("error", f"timed out: {out.detail}")
    if kind is CheckKind.TESTS:
        return CheckResult(await _tests(kind, out, junit, common), out)
    text = f"{out.stdout}\n{out.stderr}"
    ok = out.exit_code == 0
    metrics: dict[str, float] = {}
    if kind is CheckKind.LINT:
        m = _LINT_COUNT.search(text)
        metrics["lint_errors"] = float(m.group(1)) if m else (0.0 if ok else 1.0)
    elif kind is CheckKind.TYPECHECK:
        found = _TYPE_COUNT.findall(text)
        metrics["type_errors"] = float(found[-1]) if found else (0.0 if ok else 1.0)
    if ok:
        return result("passed", f"{kind.value} passed", metrics)
    return result("failed", f"{kind.value} failed: exit code {out.exit_code}", metrics)


async def _tests(
    kind: CheckKind, out: CommandOutcome, junit: Path | None, common: dict[str, object]
) -> Check:
    metrics: dict[str, float] = {}
    detail = ""
    if junit is not None and await anyio.Path(junit).exists():
        try:
            m = parse_junit(await anyio.Path(junit).read_text(encoding="utf-8"))
        except ParseError as exc:
            return _check(kind, "error", f"cannot read test report: {exc}", **common)  # type: ignore[arg-type]
        metrics = m.metrics()
        if m.failed:
            names = ", ".join(m.failed_names[:5])
            detail = f"{m.failed} of {m.total} tests failed ({names})"
    if out.exit_code == PYTEST_NO_TESTS and not metrics.get("tests_total"):
        return _check(kind, "warning", "no tests collected", metrics=metrics, **common)  # type: ignore[arg-type]
    if out.exit_code == 0 and not metrics.get("tests_failed"):
        summary = f"{int(metrics['tests_passed'])} tests passed" if metrics else "tests passed"
        return _check(kind, "passed", summary, metrics=metrics, **common)  # type: ignore[arg-type]
    summary = detail or f"tests failed: exit code {out.exit_code}"
    return _check(kind, "failed", summary, metrics=metrics, **common)  # type: ignore[arg-type]
