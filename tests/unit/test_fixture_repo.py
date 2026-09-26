from __future__ import annotations

import json
from pathlib import Path

import pytest

import repos

PATCHES = ["hello.diff", "auth_v1_broken.diff", "auth_v2_fixed.diff", "planted_secret.diff",
           "outside_scope.diff"]  # fmt: skip


def test_materialize_makes_a_clean_committed_git_repo(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "r")
    assert repos.git(repo, "status", "--porcelain").stdout == ""
    assert repos.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert len(repos.git(repo, "rev-parse", "HEAD").stdout.strip()) == 40


def test_baseline_tests_pass(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "r")
    res = repos.run_pytest(repo)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize("name", PATCHES)
def test_every_patch_applies_cleanly_to_baseline(tmp_path: Path, name: str) -> None:
    repo = repos.materialize_sample_py(tmp_path / "r")
    repos.apply_patch(repo, name)
    assert repos.git(repo, "status", "--porcelain").stdout != ""


def test_hello_patch_adds_endpoint_and_tests_pass(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "r")
    repos.apply_patch(repo, "hello.diff")
    res = repos.run_pytest(repo)
    assert res.returncode == 0, res.stdout
    assert "6 passed" in res.stdout


def test_broken_jwt_patch_fails_its_own_tests(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "r")
    repos.apply_patch(repo, "auth_v1_broken.diff")
    res = repos.run_pytest(repo)
    assert res.returncode != 0
    assert "test_expired_token_rejected" in res.stdout


def test_fixed_jwt_patch_passes(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "r")
    repos.apply_patch(repo, "auth_v2_fixed.diff")
    res = repos.run_pytest(repo)
    assert res.returncode == 0, res.stdout


def test_patches_leave_the_baseline_lint_clean(tmp_path: Path) -> None:
    import subprocess
    import sys

    for name in ("hello.diff", "auth_v2_fixed.diff"):
        repo = repos.materialize_sample_py(tmp_path / name.replace(".", "_"))
        repos.apply_patch(repo, name)
        res = subprocess.run(
            [sys.executable, "-m", "ruff", "check", "."], cwd=repo, capture_output=True, text=True
        )
        assert res.returncode == 0, res.stdout


def test_secret_patch_contains_an_aws_shaped_fake_key() -> None:
    text = (repos.PATCHES / "planted_secret.diff").read_text()
    assert "AKIAIOSFODNN7EXAMPLE" in text and "EXAMPLE" in text


def test_outside_scope_patch_touches_deploy_directory() -> None:
    text = (repos.PATCHES / "outside_scope.diff").read_text()
    assert "b/deploy/production.yaml" in text and "b/app/handler.py" in text


def test_review_findings_fixture_is_well_formed() -> None:
    doc = json.loads(
        (repos.FIXTURES / "agent_scripts" / "findings" / "auth_review.json").read_text()
    )
    assert {f["severity"] for f in doc["findings"]} == {"high", "medium", "low"}
    for f in doc["findings"]:
        assert set(f) == {"severity", "file", "line", "title", "detail", "confidence"}
