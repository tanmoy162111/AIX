from __future__ import annotations

import json

import pytest

from aix.verification.parsers import (
    ParseError,
    parse_gitleaks,
    parse_junit,
    parse_npm_audit,
    parse_pip_audit,
    parse_sarif,
)

JUNIT = """<?xml version="1.0"?>
<testsuites>
 <testsuite name="pytest" tests="5" failures="1" errors="1" skipped="1">
  <testcase classname="t.a" name="ok1"/>
  <testcase classname="t.a" name="ok2"/>
  <testcase classname="t.a" name="bad"><failure message="assert 1 == 2">trace</failure></testcase>
  <testcase classname="t.b" name="boom"><error message="KeyError">trace</error></testcase>
  <testcase classname="t.b" name="skip"><skipped message="later"/></testcase>
 </testsuite>
</testsuites>"""


def test_junit_counts_and_names() -> None:
    m = parse_junit(JUNIT)
    assert (m.total, m.passed, m.failed, m.skipped) == (5, 2, 2, 1)
    assert m.failed_names == ["t.a::bad", "t.b::boom"]
    assert m.metrics() == {
        "tests_total": 5.0, "tests_passed": 2.0, "tests_failed": 2.0, "tests_skipped": 1.0,
    }  # fmt: skip


def test_junit_single_suite_root_and_empty() -> None:
    m = parse_junit('<testsuite><testcase name="a"/></testsuite>')
    assert (m.total, m.passed) == (1, 1)
    assert parse_junit("<testsuites/>").total == 0


@pytest.mark.parametrize("bad", ["", "not xml", "<a><b></a>", "<html/>"])
def test_junit_rejects_garbage(bad: str) -> None:
    with pytest.raises(ParseError):
        parse_junit(bad)


def test_junit_does_not_expand_entities() -> None:
    evil = '<!DOCTYPE x [<!ENTITY a "boom">]><testsuite><testcase name="&a;"/></testsuite>'
    with pytest.raises(ParseError):
        parse_junit(evil)


SARIF = {
    "version": "2.1.0",
    "runs": [
        {
            "tool": {"driver": {"name": "semgrep", "rules": [
                {"id": "r.sqli", "properties": {"security-severity": "9.1"}},
                {"id": "r.md5", "properties": {"security-severity": "5.0"}},
            ]}},
            "results": [
                {"ruleId": "r.sqli", "level": "error", "message": {"text": "SQL injection"},
                 "locations": [{"physicalLocation": {"artifactLocation": {"uri": "app/db.py"},
                                                     "region": {"startLine": 12}}}]},
                {"ruleId": "r.md5", "level": "warning", "message": {"text": "weak hash"}},
                {"ruleId": "r.other", "level": "error", "message": {"text": "generic"}},
                {"ruleId": "r.note", "level": "note", "message": {"text": "fyi"}},
            ],
        }
    ],
}  # fmt: skip


def test_sarif_severity_from_cvss_then_level() -> None:
    f = parse_sarif(json.dumps(SARIF))
    assert [x.severity for x in f.findings] == ["critical", "medium", "high", "low"]
    assert f.findings[0].path == "app/db.py" and f.findings[0].line == 12
    assert f.counts() == {
        "findings_critical": 1.0, "findings_high": 1.0, "findings_medium": 1.0,
        "findings_low": 1.0,
    }  # fmt: skip


def test_sarif_empty_and_invalid() -> None:
    assert parse_sarif('{"runs": []}').findings == []
    for bad in ("", "[]", '{"runs": 3}', "{oops"):
        with pytest.raises(ParseError):
            parse_sarif(bad)


def test_gitleaks_every_finding_is_critical() -> None:
    doc = [{"RuleID": "aws-access-token", "File": "a.py", "StartLine": 3, "Description": "AWS key",
            "Secret": "AKIAXXXXXXXXXXXXXXXX", "Match": "key=AKIA..."}]  # fmt: skip
    f = parse_gitleaks(json.dumps(doc))
    assert [x.severity for x in f.findings] == ["critical"]
    assert f.findings[0].path == "a.py" and f.findings[0].rule == "aws-access-token"
    assert "AKIAXXXX" not in f.findings[0].message  # the secret itself is never carried along
    assert parse_gitleaks("[]").findings == [] and parse_gitleaks("null").findings == []
    with pytest.raises(ParseError):
        parse_gitleaks('{"a": 1}')


def test_pip_audit() -> None:
    doc = {"dependencies": [
        {"name": "flask", "version": "0.5", "vulns": [
            {"id": "PYSEC-1", "fix_versions": ["1.0"], "description": "bad"}]},
        {"name": "ok", "version": "1", "vulns": []},
    ]}  # fmt: skip
    f = parse_pip_audit(json.dumps(doc))
    assert [(x.rule, x.severity) for x in f.findings] == [("PYSEC-1", "high")]
    assert "flask" in f.findings[0].message
    with pytest.raises(ParseError):
        parse_pip_audit('{"nope": 1}')


def test_npm_audit_uses_metadata_counts() -> None:
    doc = {"vulnerabilities": {"lodash": {"severity": "moderate", "via": ["x"]},
                               "x": {"severity": "critical", "via": []}},
           "metadata": {"vulnerabilities": {"info": 0, "low": 0, "moderate": 1, "high": 0,
                                            "critical": 1, "total": 2}}}  # fmt: skip
    f = parse_npm_audit(json.dumps(doc))
    assert sorted(x.severity for x in f.findings) == ["critical", "medium"]
    assert f.counts()["findings_critical"] == 1.0
    with pytest.raises(ParseError):
        parse_npm_audit("[]")
