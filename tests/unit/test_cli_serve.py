"""`aix serve` wiring (PLAYBOOK §24): loopback default, remote opt-in, token printing."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import repos
from aix.cli.main import app
from cli_env import hermetic

runner = CliRunner()


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic(tmp_path, monkeypatch)
    monkeypatch.delenv("AIX_AGENT_CONTEXT", raising=False)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    return proj


def test_serve_binds_loopback_by_default(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    seen: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda application, **kw: seen.update(kw, app=application))
    result = runner.invoke(app, ["serve", "--project", str(project)])
    assert result.exit_code == 0, result.output
    assert seen["host"] == "127.0.0.1" and seen["port"] == 8765
    assert "127.0.0.1:8765" in result.output


def test_serve_refuses_a_public_interface_without_the_flag(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    seen: list[object] = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: seen.append(kw))
    bad = runner.invoke(app, ["serve", "--host", "0.0.0.0", "--project", str(project)])
    assert bad.exit_code == 2 and "--allow-remote" in bad.output and not seen
    ok = runner.invoke(
        app, ["serve", "--host", "0.0.0.0", "--allow-remote", "--project", str(project)]
    )
    assert ok.exit_code == 0 and seen


def test_serve_requires_an_initialized_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hermetic(tmp_path, monkeypatch)
    proj = repos.materialize_sample_py(tmp_path / "raw")
    result = runner.invoke(app, ["serve", "--project", str(proj)])
    assert result.exit_code == 2 and "aix init" in result.output


def test_show_token_prints_a_stable_token_and_is_refused_for_agents(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = runner.invoke(app, ["serve", "--show-token"])
    second = runner.invoke(app, ["serve", "--show-token"])
    assert first.exit_code == 0 and first.output.strip() == second.output.strip()
    assert len(first.output.strip()) >= 32
    approval = runner.invoke(app, ["approvals", "--show-token"])
    assert approval.output.strip() != first.output.strip()
    monkeypatch.setenv("AIX_AGENT_CONTEXT", "1")
    refused = runner.invoke(app, ["serve", "--show-token"])
    assert refused.exit_code == 2 and "agent context" in refused.output
