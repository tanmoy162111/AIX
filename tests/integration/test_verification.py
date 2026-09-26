"""`aix verify` and `aix review` on a real repository (M4.10, §17, §23.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import repos
from aix.cli.main import app

runner = CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("AIX_FAKE_SCRIPTS", raising=False)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    repos.git(proj, "add", ".gitignore")  # init writes it; a clean tree must really be clean
    repos.git(proj, "commit", "-qm", "ignore aix runtime files")
    return proj


def verify(repo: Path, *args: str) -> tuple[int, str]:
    r = runner.invoke(app, ["verify", "--path", str(repo), *args])
    return r.exit_code, r.output


def test_clean_repo_passes(repo: Path) -> None:
    code, out = verify(repo)
    assert code == 0, out
    assert "PASSED" in out
    for kind in ("build", "tests", "policy", "secrets"):
        assert kind in out


def test_json_report(repo: Path) -> None:
    code, out = verify(repo, "--json")
    doc = json.loads(out)
    assert code == 0 and doc["overall"] == "passed"
    kinds = {c["kind"]: c for c in doc["checks"]}
    assert kinds["tests"]["metrics"]["tests_total"] > 0 and kinds["tests"]["required"]
    assert kinds["build"]["command"]  # the detected command is recorded


def test_broken_change_fails_and_names_the_test(repo: Path) -> None:
    repos.apply_patch(repo, "auth_v1_broken.diff")
    code, out = verify(repo)
    assert code == 1 and "FAILED" in out and "tests" in out


def test_untracked_secret_is_caught_and_not_printed(repo: Path) -> None:
    repos.apply_patch(repo, "planted_secret.diff")  # creates an untracked file
    code, out = verify(repo)
    assert code == 1 and "secrets" in out and "aws-access-key-id" in out
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    doc = json.loads(verify(repo, "--json")[1])
    assert "AKIAIOSFODNN7EXAMPLE" not in json.dumps(doc)


def test_forbidden_file_fails_policy(repo: Path) -> None:
    (repo / ".env").write_text("X=1\n")
    code, out = verify(repo)
    assert code == 1 and "forbidden file type" in out


def test_explicit_commands_win_and_missing_tool_is_incomplete(repo: Path) -> None:
    cfg = repo / ".aix" / "config.yaml"
    cfg.write_text(yaml.safe_dump({"verification": {"commands": {"lint": ["no-such-linter-xyz"]}}}))
    code, out = verify(repo)
    assert code == 1 and "INCOMPLETE" in out and "tool not available" in out


def test_outside_a_git_repo_only_command_checks_run(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    plain = tmp_path / "plain"
    plain.mkdir()
    code, out = verify(plain)
    assert code == 1 and "INCOMPLETE" in out  # nothing detected, required checks did not run


# ---- aix review ------------------------------------------------------------------------------


def review(repo: Path, *args: str) -> tuple[int, str]:
    r = runner.invoke(app, ["review", "--path", str(repo), *args])
    return r.exit_code, r.output


def script(tmp: Path, findings: list[dict[str, object]]) -> Path:
    path = tmp / "reviewer.yaml"
    path.write_text(
        yaml.safe_dump(
            {"match": {"task_type": "review"}, "attempts": [{"emit_findings": findings}]}
        )
    )
    return path


HIGH = {"severity": "high", "file": "app/auth.py", "line": 3, "title": "No expiry check",
        "detail": "tokens never expire", "confidence": 0.9}  # fmt: skip


def test_review_reports_findings_and_fails_on_a_confident_high(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos.apply_patch(repo, "auth_v1_broken.diff")
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(script(tmp_path, [HIGH])))
    code, out = review(repo, "--agent", "fake")
    assert code == 1 and "No expiry check" in out and "fake" in out
    assert list((repo / ".aix" / "worktrees").glob("*")) == []


def test_review_with_no_findings_passes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos.apply_patch(repo, "hello.diff")
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(script(tmp_path, [])))
    code, out = review(repo, "--agent", "fake", "--json")
    doc = json.loads(out)
    assert code == 0 and doc["status"] == "passed" and doc["reviewer"] == "fake"


def test_review_of_a_clean_tree_says_so(repo: Path) -> None:
    code, out = review(repo, "--agent", "fake")
    assert code == 0 and "nothing to review" in out.lower()


def test_review_base_ref_reviews_committed_changes(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos.apply_patch(repo, "hello.diff")
    repos.git(repo, "add", "-A")
    repos.git(repo, "commit", "-qm", "hello")
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(script(tmp_path, [])))
    assert "nothing to review" in review(repo, "--agent", "fake")[1].lower()
    code, out = review(repo, "--agent", "fake", "--base", "HEAD~1")
    assert code == 0 and "nothing to review" not in out.lower()


def test_review_unknown_agent(repo: Path) -> None:
    repos.apply_patch(repo, "hello.diff")
    code, out = review(repo, "--agent", "nope")
    assert code == 2 and "unknown agent" in out
