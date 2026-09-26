"""Security checks: secrets, SAST, dependency audit (PLAYBOOK §17.3).

``secrets`` always runs the builtin scanner over the patch (deterministic, no dependency, see
ADR-0016) and additionally merges gitleaks findings when gitleaks is installed. SAST and deps use
external tools when present; a missing tool is ``skipped``. Finding text never contains secret
values.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal

import anyio

from aix.domain.enums import CheckKind
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check, CheckStatus
from aix.security.redact import SECRET_PATTERNS
from aix.verification.checks import CheckResult
from aix.verification.detect import detect_toolchain
from aix.verification.parsers import (
    Finding,
    Findings,
    ParseError,
    parse_bandit,
    parse_gitleaks,
    parse_npm_audit,
    parse_pip_audit,
    parse_sarif,
)
from aix.verification.runner import CommandOutcome, run_command

Mode = Literal["auto", "on", "off"]

_HUNK: Final = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def scan_patch_for_secrets(patch: str) -> Findings:
    """Scan the *added* lines of a unified diff for secret-shaped strings (all ``critical``)."""
    findings: list[Finding] = []
    path: str | None = None
    line_no = 0
    for raw in patch.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            continue
        if raw.startswith("--- ") or raw.startswith("diff "):
            continue
        if m := _HUNK.match(raw):
            line_no = int(m.group(1)) - 1
            continue
        if raw.startswith("-"):
            continue
        line_no += 1
        if not raw.startswith("+"):
            continue
        text = raw[1:]
        for rule, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(
                    Finding("critical", rule, f"possible {rule.replace('-', ' ')}", path, line_no)
                )
                break
    return Findings(findings)


def _find(tool: str, path_env: str | None, which: Callable[[str], str | None] | None) -> bool:
    if which is not None:
        return which(tool) is not None
    return shutil.which(tool, path=path_env) is not None


def _describe(findings: Findings, limit: int = 5) -> str:
    counts = ", ".join(
        f"{int(n)} {k.removeprefix('findings_')}" for k, n in findings.counts().items()
    )
    shown = "; ".join(
        f"{f.rule}" + (f" {f.path}" + (f":{f.line}" if f.line else "") if f.path else "")
        for f in findings.findings[:limit]
    )
    more = f"; +{len(findings.findings) - limit} more" if len(findings.findings) > limit else ""
    return f"{len(findings.findings)} finding(s) ({counts}): {shown}{more}"


def _check(
    kind: CheckKind,
    status: CheckStatus,
    summary: str,
    *,
    required: bool,
    findings: Findings | None = None,
    argv: list[str] | None = None,
    duration_ms: int = 0,
) -> Check:
    worst = findings.worst() if findings else None
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=kind,
        status=status,
        severity=(worst or "info") if status in ("failed", "warning") else "info",
        required=required,
        summary=summary,
        metrics=findings.counts() if findings else {},
        command=argv,
        duration_ms=duration_ms,
    )


def _from_findings(
    kind: CheckKind, findings: Findings, *, required: bool, argv: list[str] | None, ms: int
) -> Check:
    if not findings.findings:
        return _check(
            kind,
            "passed",
            f"{kind.value}: no findings",
            required=required,
            argv=argv,
            duration_ms=ms,
        )
    blocking = findings.worst() in ("high", "critical")
    return _check(
        kind,
        "failed" if blocking else "warning",
        _describe(findings),
        required=required,
        findings=findings,
        argv=argv,
        duration_ms=ms,
    )


async def _read(path: Path) -> str | None:
    p = anyio.Path(path)
    return await p.read_text(encoding="utf-8") if await p.exists() else None


# ---- secrets ---------------------------------------------------------------------------------


async def run_secrets_check(
    patch: str,
    cwd: Path,
    *,
    mode: Mode,
    out_dir: Path | None = None,
    path_env: str | None = None,
    which: Callable[[str], str | None] | None = None,
    timeout_s: float = 300,
) -> CheckResult | None:
    """Builtin scan of ``patch`` plus gitleaks over ``cwd`` when installed. ``None`` if ``off``.

    Contract: required; any finding is ``failed`` with severity ``critical``. If gitleaks is
    present but errors, the builtin result still stands and the summary says so.
    """
    if mode == "off":
        return None
    findings = list(scan_patch_for_secrets(patch).findings)
    argv: list[str] | None = None
    note = ""
    outcome: CommandOutcome | None = None
    if _find("gitleaks", path_env, which):
        report = (out_dir or cwd / ".aix-tmp") / "gitleaks.json"
        await anyio.Path(report.parent).mkdir(parents=True, exist_ok=True)
        argv = [
            "gitleaks",
            "detect",
            "--no-git",
            "--source",
            ".",
            "--redact",
            "--report-format",
            "json",
            "--report-path",
            str(report),
        ]
        outcome = await run_command(
            argv,
            cwd,
            allow=["gitleaks"],
            timeout_s=timeout_s,
            env={"PATH": path_env} if path_env else None,
            out_dir=out_dir,
            name="secrets",
        )
        text = await _read(report)
        if outcome.status != "exited" or (outcome.exit_code not in (0, 1)):
            note = f" (gitleaks {outcome.status}: exit {outcome.exit_code})"
        elif text is not None:
            try:
                findings += parse_gitleaks(text).findings
            except ParseError as exc:
                note = f" (gitleaks report unreadable: {exc})"
    result = Findings(findings)
    ms = outcome.duration_ms if outcome else 0
    if not findings:
        check = _check(
            CheckKind.SECRETS,
            "passed",
            f"no secrets found{note}",
            required=True,
            argv=argv,
            duration_ms=ms,
        )
    else:
        check = _check(
            CheckKind.SECRETS,
            "failed",
            _describe(result) + note,
            required=True,
            findings=result,
            argv=argv,
            duration_ms=ms,
        )
    return CheckResult(check, outcome)


# ---- SAST and deps ----------------------------------------------------------------------------


def _skipped(kind: CheckKind, why: str, required: bool) -> CheckResult:
    return CheckResult(_check(kind, "skipped", why, required=required))


async def _run_tool(
    argv: list[str],
    cwd: Path,
    *,
    path_env: str | None,
    out_dir: Path | None,
    name: str,
    timeout_s: float,
    network: str,
) -> CommandOutcome:
    return await run_command(
        argv,
        cwd,
        allow=[argv[0]],
        timeout_s=timeout_s,
        network=network,
        env={"PATH": path_env} if path_env else None,
        out_dir=out_dir,
        name=name,
    )


async def run_sast_check(
    cwd: Path,
    *,
    mode: Mode,
    out_dir: Path | None = None,
    path_env: str | None = None,
    timeout_s: float = 600,
) -> CheckResult | None:
    """bandit (Python projects) and semgrep (any project) when installed. ``None`` if ``off``.

    Required only when ``mode == "on"``; ``high``/``critical`` findings fail, lower ones warn.
    semgrep uses a local config file when the repo has one, otherwise ``--config auto`` with
    network access (rule download).
    """
    if mode == "off":
        return None
    required = mode == "on"
    findings: list[Finding] = []
    ran: list[str] = []
    ms = 0
    if "python" in detect_toolchain(cwd).ecosystems and _find("bandit", path_env, None):
        argv = ["bandit", "-r", ".", "-f", "json", "-q"]
        out = await _run_tool(
            argv,
            cwd,
            path_env=path_env,
            out_dir=out_dir,
            name="bandit",
            timeout_s=timeout_s,
            network="deny",
        )
        ms += out.duration_ms
        try:
            findings += parse_bandit(out.stdout).findings
            ran.append("bandit")
        except ParseError as exc:
            return CheckResult(
                _check(
                    CheckKind.SECURITY_SAST,
                    "error",
                    f"bandit output: {exc}",
                    required=required,
                    argv=argv,
                ),
                out,
            )
    if _find("semgrep", path_env, None):
        sarif = (out_dir or cwd / ".aix-tmp") / "semgrep.sarif"
        await anyio.Path(sarif.parent).mkdir(parents=True, exist_ok=True)
        local = next((n for n in (".semgrep.yml", "semgrep.yml") if (cwd / n).is_file()), None)
        argv = [
            "semgrep",
            "scan",
            "--config",
            local or "auto",
            "--metrics",
            "off",
            "--quiet",
            "--sarif",
            "--output",
            str(sarif),
            ".",
        ]
        out = await _run_tool(
            argv,
            cwd,
            path_env=path_env,
            out_dir=out_dir,
            name="semgrep",
            timeout_s=timeout_s,
            network="deny" if local else "allow",
        )
        ms += out.duration_ms
        text = await _read(sarif)
        try:
            if text is None:
                raise ParseError("no SARIF report was written")
            findings += parse_sarif(text).findings
            ran.append("semgrep")
        except ParseError as exc:
            return CheckResult(
                _check(
                    CheckKind.SECURITY_SAST,
                    "error",
                    f"semgrep output: {exc}",
                    required=required,
                    argv=argv,
                ),
                out,
            )
    if not ran:
        return _skipped(CheckKind.SECURITY_SAST, "tool not available: semgrep, bandit", required)
    return CheckResult(
        _from_findings(
            CheckKind.SECURITY_SAST, Findings(findings), required=required, argv=ran, ms=ms
        )
    )


async def run_deps_check(
    cwd: Path,
    *,
    mode: Mode,
    out_dir: Path | None = None,
    path_env: str | None = None,
    timeout_s: float = 600,
) -> CheckResult | None:
    """pip-audit (Python manifests) and ``npm audit`` (package.json). ``None`` if ``off``.

    Audits query vulnerability databases, so they run with network access (ADR-0016). Required
    only when ``mode == "on"``; ``high``/``critical`` fail, others warn; unparseable output is
    ``error``.
    """
    if mode == "off":
        return None
    required = mode == "on"
    plans: list[tuple[str, list[str], Callable[[str], Findings]]] = []
    if (cwd / "requirements.txt").is_file():
        plans.append(
            (
                "pip-audit",
                ["pip-audit", "--format", "json", "-r", "requirements.txt"],
                parse_pip_audit,
            )
        )
    elif (cwd / "pyproject.toml").is_file():
        plans.append(("pip-audit", ["pip-audit", "--format", "json"], parse_pip_audit))
    if (cwd / "package.json").is_file():
        plans.append(("npm", ["npm", "audit", "--json"], parse_npm_audit))
    if not plans:
        return _skipped(CheckKind.DEPS, "no dependency manifest found", required)
    findings: list[Finding] = []
    ran: list[str] = []
    ms = 0
    for tool, argv, parse in plans:
        if not _find(tool, path_env, None):
            continue
        out = await _run_tool(
            argv,
            cwd,
            path_env=path_env,
            out_dir=out_dir,
            name=tool.replace("-", "_"),
            timeout_s=timeout_s,
            network="allow",
        )
        ms += out.duration_ms
        if out.status != "exited":
            return CheckResult(
                _check(
                    CheckKind.DEPS,
                    "error",
                    f"{tool}: {out.status} {out.detail}",
                    required=required,
                    argv=argv,
                ),
                out,
            )
        try:
            findings += parse(out.stdout).findings
            ran.append(tool)
        except ParseError as exc:
            return CheckResult(
                _check(
                    CheckKind.DEPS, "error", f"{tool} output: {exc}", required=required, argv=argv
                ),
                out,
            )
    if not ran:
        names = ", ".join(t for t, _, _ in plans)
        return _skipped(CheckKind.DEPS, f"tool not available: {names}", required)
    return CheckResult(
        _from_findings(CheckKind.DEPS, Findings(findings), required=required, argv=ran, ms=ms)
    )
