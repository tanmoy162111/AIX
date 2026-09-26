"""Parsers for tool output that checks depend on (PLAYBOOK §17.2).

All parsers are pure and raise :class:`ParseError` on input they cannot understand, so the calling
check ends ``error`` instead of silently passing. Secret values in gitleaks output are never
copied into a finding.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from xml.etree import ElementTree as ET

Severity = Literal["info", "low", "medium", "high", "critical"]
_ORDER: Final[tuple[Severity, ...]] = ("critical", "high", "medium", "low")


class ParseError(ValueError):
    """Tool output was not in the expected format."""


@dataclass(frozen=True)
class TestMetrics:
    """Counts from a JUnit report."""

    __test__ = False  # not a pytest test class

    total: int
    passed: int
    failed: int
    skipped: int
    failed_names: list[str] = field(default_factory=list[str])

    def metrics(self) -> dict[str, float]:
        """Metric names from §17.2."""
        return {
            "tests_total": float(self.total),
            "tests_passed": float(self.passed),
            "tests_failed": float(self.failed),
            "tests_skipped": float(self.skipped),
        }


@dataclass(frozen=True)
class Finding:
    severity: Severity
    rule: str
    message: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class Findings:
    findings: list[Finding] = field(default_factory=list[Finding])

    def counts(self) -> dict[str, float]:
        """``findings_<severity>`` for every severity that occurs (info is not counted)."""
        return {
            f"findings_{sev}": float(n)
            for sev in _ORDER
            if (n := sum(1 for f in self.findings if f.severity == sev))
        }

    def worst(self) -> Severity | None:
        for sev in _ORDER:
            if any(f.severity == sev for f in self.findings):
                return sev
        return None


def _json(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ParseError(f"{what}: not valid JSON: {exc}") from exc


# ---- JUnit -----------------------------------------------------------------------------------


def parse_junit(xml: str) -> TestMetrics:
    """Count testcases in a JUnit XML report (``<testsuites>`` or a single ``<testsuite>``).

    Documents that declare a DTD or entities are rejected (no entity expansion).
    """
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise ParseError("junit: DTDs and entities are not accepted")
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ParseError(f"junit: {exc}") from exc
    if root.tag not in ("testsuites", "testsuite"):
        raise ParseError(f"junit: unexpected root element <{root.tag}>")
    total = passed = failed = skipped = 0
    failed_names: list[str] = []
    for case in root.iter("testcase"):
        total += 1
        if case.find("failure") is not None or case.find("error") is not None:
            failed += 1
            failed_names.append(f"{case.get('classname', '')}::{case.get('name', '')}".strip(":"))
        elif case.find("skipped") is not None:
            skipped += 1
        else:
            passed += 1
    return TestMetrics(total, passed, failed, skipped, failed_names)


# ---- SARIF -----------------------------------------------------------------------------------


def _cvss(score: float) -> Severity:
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    return "low"


_LEVEL: Final[dict[str, Severity]] = {"error": "high", "warning": "medium", "note": "low"}


def _d(value: Any) -> dict[str, Any]:
    """``value`` if it is an object, else an empty one (lenient access to optional sections)."""
    return value if isinstance(value, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def _l(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []  # pyright: ignore[reportUnknownVariableType]


def _score(value: Any) -> float | None:
    return float(value) if value is not None and re.fullmatch(r"\d+(\.\d+)?", str(value)) else None


def parse_sarif(text: str) -> Findings:
    """Findings from SARIF 2.x. Severity: ``security-severity`` (CVSS) if given, else the level."""
    doc = _json(text, "sarif")
    if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):  # pyright: ignore[reportUnknownMemberType]
        raise ParseError("sarif: expected an object with a 'runs' list")
    out: list[Finding] = []
    for run in _l(doc["runs"]):
        if not isinstance(run, dict):
            raise ParseError("sarif: run is not an object")
        run_d = _d(run)
        scores: dict[str, float] = {}
        for rule in _l(_d(_d(run_d.get("tool")).get("driver")).get("rules")):
            score = _score(_d(_d(rule).get("properties")).get("security-severity"))
            if score is not None:
                scores[str(_d(rule).get("id"))] = score
        for res in _l(run_d.get("results")):
            r = _d(res)
            rule_id = str(r.get("ruleId", ""))
            score = scores.get(rule_id)
            if score is None:
                score = _score(_d(r.get("properties")).get("security-severity"))
            level = _LEVEL.get(str(r.get("level", "warning")), "medium")
            phys = _d(_d((_l(r.get("locations")) or [{}])[0]).get("physicalLocation"))
            uri = _d(phys.get("artifactLocation")).get("uri")
            line = _d(phys.get("region")).get("startLine")
            out.append(
                Finding(
                    _cvss(score) if score is not None else level,
                    rule_id,
                    str(_d(r.get("message")).get("text", "")),
                    uri if isinstance(uri, str) else None,
                    line if isinstance(line, int) else None,
                )
            )
    return Findings(out)


# ---- gitleaks --------------------------------------------------------------------------------


def parse_gitleaks(text: str) -> Findings:
    """Every gitleaks finding is critical (§17.3). The secret and match text are dropped."""
    doc = _json(text, "gitleaks")
    if doc is None:
        return Findings()
    if not isinstance(doc, list):
        raise ParseError("gitleaks: expected a JSON list")
    out: list[Finding] = []
    for item in _l(doc):
        i = _d(item)
        if not i:
            continue
        file, line = i.get("File"), i.get("StartLine")
        out.append(
            Finding(
                "critical",
                str(i.get("RuleID", "secret")),
                str(i.get("Description", "possible secret")),
                file if isinstance(file, str) else None,
                line if isinstance(line, int) else None,
            )
        )
    return Findings(out)


# ---- dependency audits -----------------------------------------------------------------------


def parse_pip_audit(text: str) -> Findings:
    """pip-audit JSON. It reports no severity, so every vulnerability is treated as ``high``."""
    doc = _json(text, "pip-audit")
    deps = _d(doc).get("dependencies")
    if not isinstance(deps, list):
        raise ParseError("pip-audit: expected an object with a 'dependencies' list")
    out: list[Finding] = []
    for dep in _l(deps):
        d = _d(dep)
        for vuln in _l(d.get("vulns")):
            v = _d(vuln)
            fixes = ", ".join(str(x) for x in _l(v.get("fix_versions")))
            msg = f"{d.get('name')} {d.get('version')}: {v.get('description', 'vulnerable')}"
            out.append(
                Finding(
                    "high", str(v.get("id", "vuln")), msg + (f" (fix: {fixes})" if fixes else "")
                )
            )
    return Findings(out)


_NPM: Final[dict[str, Severity]] = {
    "critical": "critical", "high": "high", "moderate": "medium", "low": "low", "info": "info",
}  # fmt: skip


def parse_npm_audit(text: str) -> Findings:
    """``npm audit --json`` (npm 7+): one finding per vulnerable package."""
    doc = _json(text, "npm audit")
    vulns = _d(doc).get("vulnerabilities")
    if not isinstance(vulns, dict):
        raise ParseError("npm audit: expected an object with a 'vulnerabilities' map")
    out = [
        Finding(
            _NPM.get(str(_d(v).get("severity")), "medium"), str(n), f"{n}: vulnerable dependency"
        )
        for n, v in _d(vulns).items()
        if isinstance(v, dict)
    ]
    return Findings(out)
