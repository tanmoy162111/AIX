from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import repos
from aix.domain.enums import CheckKind as K
from aix.verification.parsers import ParseError, parse_bandit
from aix.verification.security import (
    run_deps_check,
    run_sast_check,
    run_secrets_check,
    scan_patch_for_secrets,
)
from binaries import env_with_path, make_fake_binary

pytestmark = pytest.mark.anyio
PLANTED = (repos.PATCHES / "planted_secret.diff").read_text()


def add(path: str, *lines: str) -> str:
    body = "".join(f"+{x}\n" for x in lines)
    head = f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n"
    return f"{head}@@ -0,0 +1,{len(lines)} @@\n{body}"


# ---- builtin scanner --------------------------------------------------------------------------


def test_planted_aws_key_is_found_with_path_and_line_but_never_echoed() -> None:
    f = scan_patch_for_secrets(PLANTED)
    assert f.worst() == "critical" and {x.rule for x in f.findings} >= {"aws-access-key-id"}
    hit = next(x for x in f.findings if x.rule == "aws-access-key-id")
    assert hit.path == "app/config.py" and hit.line == 4
    assert "AKIAIOSFODNN7EXAMPLE" not in hit.message


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("token = 'ghp_" + "a" * 36 + "'", "github-token"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private-key"),
        ("slack = 'xoxb-1234567890-abcdefghij'", "slack-token"),
        ("GOOGLE = 'AIza" + "B" * 35 + "'", "google-api-key"),
        ("password = 'correct-horse-battery-staple'", "generic-secret"),
        ('"api_key": "0123456789abcdef0123"', "generic-secret"),
    ],
)
def test_other_secret_shapes(line: str, rule: str) -> None:
    f = scan_patch_for_secrets(add("x.py", line))
    assert rule in {x.rule for x in f.findings}


@pytest.mark.parametrize(
    "line",
    ["password = get_password()", "token = os.environ['TOKEN']", "x = 'short'", "# api_key docs"],
)
def test_ordinary_code_is_not_flagged(line: str) -> None:
    assert scan_patch_for_secrets(add("x.py", line)).findings == []


def test_only_added_lines_are_scanned() -> None:
    old = "AKIAIOSFODNN7EXAMPLE"
    patch = f"--- a/x\n+++ b/x\n@@ -1 +1 @@\n-{old}\n+clean\n context {old}\n"
    assert scan_patch_for_secrets(patch).findings == []


# ---- secrets check ----------------------------------------------------------------------------


async def test_secrets_check_fails_on_the_planted_key(tmp_path: Path) -> None:
    r = await run_secrets_check(PLANTED, tmp_path, mode="auto", which=lambda _b: None)
    assert r is not None and r.check.kind is K.SECRETS
    assert r.check.status == "failed" and r.check.severity == "critical" and r.check.required
    assert r.check.metrics["findings_critical"] >= 1
    assert "AKIAIOSFODNN7EXAMPLE" not in r.check.summary


async def test_secrets_check_passes_a_clean_patch_and_off_skips(tmp_path: Path) -> None:
    ok = await run_secrets_check(add("a.py", "x = 1"), tmp_path, mode="auto", which=lambda _b: None)
    assert ok is not None and ok.check.status == "passed"
    assert await run_secrets_check(PLANTED, tmp_path, mode="off") is None


async def test_gitleaks_findings_are_merged_when_installed(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    make_fake_binary(
        bin_dir,
        "gitleaks",
        """
        import json, sys
        args = sys.argv[1:]
        report = args[args.index("--report-path") + 1]
        leak = {"RuleID": "stripe", "File": "b.py", "StartLine": 2, "Description": "Stripe key"}
        json.dump([leak], open(report, "w"))
        sys.exit(1)
        """,
    )
    env = env_with_path(bin_dir)
    r = await run_secrets_check(
        add("a.py", "x = 1"), tmp_path, mode="on", out_dir=tmp_path / "o", path_env=env["PATH"]
    )
    assert r is not None and r.check.status == "failed"
    assert "stripe" in r.check.summary and r.check.command and r.check.command[0] == "gitleaks"


# ---- sast / deps -----------------------------------------------------------------------------


def test_parse_bandit() -> None:
    doc = {"results": [{"issue_severity": "HIGH", "test_id": "B602", "filename": "a.py",
                        "line_number": 7, "issue_text": "shell=True"},
                       {"issue_severity": "LOW", "test_id": "B101", "filename": "b.py",
                        "line_number": 1, "issue_text": "assert"}]}  # fmt: skip
    f = parse_bandit(json.dumps(doc))
    assert [x.severity for x in f.findings] == ["high", "low"] and f.findings[0].line == 7
    with pytest.raises(ParseError):
        parse_bandit("{}")


def tool(bin_dir: Path, name: str, body: str) -> str:
    make_fake_binary(bin_dir, name, body)
    return env_with_path(bin_dir)["PATH"]


async def test_sast_with_bandit(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    path = tool(
        tmp_path / "bin",
        "bandit",
        """
        import json
        print(json.dumps({"results": [{"issue_severity": "HIGH", "test_id": "B602",
              "filename": "a.py", "line_number": 3, "issue_text": "shell=True"}]}))
        raise SystemExit(1)
        """,
    )
    r = await run_sast_check(tmp_path, mode="auto", path_env=path, out_dir=tmp_path / "o")
    assert r is not None and r.check.kind is K.SECURITY_SAST
    assert r.check.status == "failed" and not r.check.required
    assert r.check.metrics == {"findings_high": 1.0}


async def test_sast_with_semgrep_sarif_and_no_tool(tmp_path: Path) -> None:
    sarif = json.dumps(
        {"runs": [{"results": [{"ruleId": "r", "level": "note", "message": {"text": "m"}}]}]}
    )
    path = tool(
        tmp_path / "bin",
        "semgrep",
        f"""
        import sys
        args = sys.argv[1:]
        open(args[args.index("--output") + 1], "w").write({sarif!r})
        """,
    )
    r = await run_sast_check(tmp_path, mode="on", path_env=path, out_dir=tmp_path / "o")
    assert r is not None and r.check.status == "warning" and r.check.required  # low finding only
    none = await run_sast_check(tmp_path, mode="auto", path_env=str(tmp_path / "empty"))
    assert none is not None and none.check.status == "skipped"
    assert await run_sast_check(tmp_path, mode="off") is None


async def test_deps_pip_audit_high_fails_and_npm_moderate_warns(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==0.5\n")
    audit = json.dumps({"dependencies": [{"name": "flask", "version": "0.5", "vulns": [
        {"id": "PYSEC-1", "fix_versions": ["1.0"], "description": "bad"}]}]})  # fmt: skip
    path = tool(tmp_path / "bin", "pip-audit", f"print({audit!r})\nraise SystemExit(1)\n")
    r = await run_deps_check(tmp_path, mode="auto", path_env=path, out_dir=tmp_path / "o")
    assert r is not None and r.check.kind is K.DEPS and r.check.status == "failed"

    (tmp_path / "requirements.txt").unlink()
    (tmp_path / "package.json").write_text("{}")
    npm = json.dumps({"vulnerabilities": {"x": {"severity": "moderate"}}})
    path = tool(tmp_path / "bin2", "npm", f"print({npm!r})\nraise SystemExit(1)\n")
    r2 = await run_deps_check(tmp_path, mode="auto", path_env=path, out_dir=tmp_path / "o2")
    assert r2 is not None and r2.check.status == "warning"


async def test_deps_unparseable_output_is_an_error(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("x\n")
    path = tool(tmp_path / "bin", "pip-audit", "print('garbage')\n")
    r = await run_deps_check(tmp_path, mode="auto", path_env=path)
    assert r is not None and r.check.status == "error"


async def test_deps_with_no_manifest_is_skipped(tmp_path: Path) -> None:
    r = await run_deps_check(tmp_path, mode="auto", path_env=sys.path[0])
    assert r is not None and r.check.status == "skipped" and "manifest" in r.check.summary


# ---- redaction --------------------------------------------------------------------------------


def test_redact_secrets_removes_values_and_keeps_context() -> None:
    from aix.security.redact import redact_secrets

    text = 'key = "AKIAIOSFODNN7EXAMPLE"\npassword = "correct-horse-battery-staple"\nx = 1\n'
    out = redact_secrets(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in out and "correct-horse" not in out
    assert 'password = "[REDACTED]"' in out and "x = 1" in out
    assert redact_secrets("nothing to hide") == "nothing to hide"
    assert redact_secrets(PLANTED).count("AKIAIOSFODNN7EXAMPLE") == 0
