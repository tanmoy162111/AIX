"""`aix run --plan-only` and `aix plan show` through the real CLI (M3.6, §13.3, §23.1)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import repos
from aix.cli.main import app
from cli_env import hermetic
from verif_env import write_fast_config

runner = CliRunner()
PLAN = {
    "tasks": [
        {"key": "a", "title": "Look around", "goal": "look", "type": "inspect"},
        {
            "key": "b",
            "title": "Build it",
            "goal": "build",
            "type": "implement",
            "depends_on": ["a"],
            "file_scope": ["src/**"],
        },
    ]
}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    hermetic(tmp_path, monkeypatch)  # never let the machine's real agent CLIs plan a test run
    monkeypatch.delenv("AIX_FAKE_SCRIPTS", raising=False)
    proj = repos.materialize_sample_py(tmp_path / "proj")
    assert runner.invoke(app, ["init", "--project", str(proj)]).exit_code == 0
    write_fast_config(proj)
    return proj


def cli(repo: Path, *args: str) -> tuple[int, str]:
    r = runner.invoke(app, [*args, "--project", str(repo)])
    return r.exit_code, r.output


def run_id_of(out: str) -> str:
    m = re.search(r"run_[0-9A-Z]{26}", out)
    assert m, out
    return m.group(0)


def test_plan_only_prints_the_plan_and_touches_no_git_state(repo: Path) -> None:
    code, out = cli(repo, "run", "Add a retry option", "--plan-only")
    assert code == 0, out
    assert "PLANNED" in out and "Planner" in out and "template" in out
    for word in ("inspect", "design", "implement", "review", "document"):
        assert word in out
    assert repos.git(repo, "branch", "--list", "aix/*").stdout.strip() == ""
    assert not (repo / ".aix" / "worktrees").exists() or not any(
        (repo / ".aix" / "worktrees").iterdir()
    )


def test_plan_only_json_and_plan_show_agree(repo: Path) -> None:
    code, out = cli(repo, "run", "Fix the crash on empty config", "--plan-only", "--json")
    assert code == 0, out
    doc = json.loads(out)
    assert doc["status"] == "planned" and doc["planner"] == "template"
    assert doc["intent"]["kind"] == "coding"
    assert {t["skill"] for t in doc["tasks"]} == {"bugfix"}
    code2, out2 = cli(repo, "plan", "show", doc["run_id"], "--json")
    assert code2 == 0, out2
    assert json.loads(out2) == doc


def test_plan_show_table(repo: Path) -> None:
    _, out = cli(repo, "run", "Add a retry option", "--plan-only")
    code, shown = cli(repo, "plan", "show", run_id_of(out))
    assert code == 0 and "implement" in shown and "depends" in shown.lower()


def test_plan_show_unknown_run(repo: Path) -> None:
    code, out = cli(repo, "plan", "show", "run_00000000000000000000000000")
    assert code == 2 and "unknown run" in out


def test_skill_flag(repo: Path) -> None:
    code, out = cli(repo, "run", "Add a retry option", "--plan-only", "--skill", "documentation")
    assert code == 0 and "documentation" in out
    code, out = cli(repo, "run", "x", "--plan-only", "--skill", "nope")
    assert code == 2 and "unknown skill" in out


def test_plan_only_with_agent_uses_it_as_the_planner(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "planner.yaml"
    script.write_text(yaml.safe_dump({"attempts": [{"planner_output": PLAN}]}))
    monkeypatch.setenv("AIX_FAKE_SCRIPTS", str(script))
    code, out = cli(repo, "run", "Add a retry option", "--plan-only", "--agent", "fake", "--json")
    assert code == 0, out
    doc = json.loads(out)
    assert doc["planner"] == "agent:fake"
    assert [t["title"] for t in doc["tasks"]] == ["Look around", "Build it"]
    assert repos.git(repo, "branch", "--list", f"aix/run/{doc['run_id']}").stdout.strip()


def test_run_without_agent_is_routed_not_a_usage_error(repo: Path) -> None:
    # M2 required --agent; since M3.8 no --agent means plan + route. The unscripted built-in fake
    # writes nothing, so the write task fails as a lazy agent, which proves the routed path ran.
    code, out = cli(repo, "run", "Add a retry option")
    assert code == 1, out
    assert "agent_no_changes" in out and "--agent" not in out
