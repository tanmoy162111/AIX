from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from typer.testing import CliRunner

from aix.cli.main import app
from cli_env import install

runner = CliRunner()


def run(env: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, [*args, "--project", str(env / "proj")])
    return result.exit_code, result.output


def agents(env: Path) -> dict[str, dict[str, object]]:
    code, out = run(env, "agent", "list", "--json")
    assert code == 0, out
    return {a["id"]: a for a in json.loads(out)}


def test_list_shows_every_builtin_even_when_unavailable(env: Path) -> None:
    got = agents(env)
    assert {"fake", "claude", "codex"} <= set(got)
    assert got["fake"]["health"] == "disabled"  # off until enabled (or AIX_FAKE_SCRIPTS is set)
    assert got["claude"]["health"] == "unavailable"
    assert "not found" in str(got["claude"]["health_reason"])


def test_list_reports_ready_agents_with_versions(env: Path) -> None:
    install(env, "claude")
    install(env, "codex")
    got = agents(env)
    assert got["claude"]["health"] == "ready" and got["claude"]["version"] == "1.2.3"
    assert got["codex"]["health"] == "ready"
    assert got["claude"]["enabled"] is True


def test_list_table_and_alias(env: Path) -> None:
    code, out = run(env, "agent", "list")
    assert code == 0 and "claude" in out and "unavailable" in out
    code2, out2 = run(env, "agents")
    assert code2 == 0 and "claude" in out2


def test_inspect(env: Path) -> None:
    install(env, "codex")
    code, out = run(env, "agent", "inspect", "codex", "--json")
    assert code == 0
    doc = json.loads(out)
    assert (
        doc["manifest"]["binary"] == "codex"
        and "OPENAI_API_KEY" in doc["manifest"]["env_allowlist"]
    )
    assert doc["spec"]["health"] == "ready" and doc["enabled"] is True
    code, out = run(env, "agent", "inspect", "nope")
    assert code == 2 and "unknown agent" in out


def test_disable_and_enable_persist_in_project_config(env: Path) -> None:
    install(env, "claude")
    code, out = run(env, "agent", "disable", "claude")
    assert code == 0 and "disabled" in out
    cfg = yaml.safe_load((env / "proj" / ".aix" / "config.yaml").read_text())
    assert "claude" not in cfg["agents"]["enabled"]
    assert agents(env)["claude"]["health"] == "disabled"
    assert agents(env)["claude"]["enabled"] is False
    code, _ = run(env, "agent", "enable", "claude")
    assert code == 0
    assert agents(env)["claude"]["health"] == "ready"


def test_enable_unknown_agent_is_a_usage_error(env: Path) -> None:
    code, out = run(env, "agent", "enable", "zzz")
    assert code == 2 and "unknown agent" in out


def test_test_fake_runs_the_smoke_prompt(env: Path) -> None:
    assert run(env, "agent", "enable", "fake")[0] == 0
    code, out = run(env, "agent", "test", "fake")
    assert code == 0, out
    assert "smoke: status=completed" in out and "claim (unverified)" in out


def test_test_without_live_only_probes_and_never_calls_the_agent(env: Path) -> None:
    install(env, "claude", "success.jsonl")
    code, out = run(env, "agent", "test", "claude")
    assert code == 0 and "pass --live" in out
    assert not (env / "log-claude" / "argv.json").exists()


def test_test_live_runs_a_read_only_smoke(env: Path) -> None:
    install(env, "claude", "success.jsonl")
    code, out = run(env, "agent", "test", "claude", "--live")
    assert code == 0, out
    argv = json.loads((env / "log-claude" / "argv.json").read_text())
    assert "dontAsk" in argv  # read-only permissions


def test_test_live_failure_exits_1_and_unavailable_exits_5(env: Path) -> None:
    install(env, "claude", "auth_failure.jsonl", exit_code=1)
    code, out = run(env, "agent", "test", "claude", "--live")
    assert code == 1 and "auth_failure" in out
    code, out = run(env, "agent", "test", "codex", "--live")  # not installed
    assert code == 5 and "unavailable" in out


def test_invalid_config_is_a_usage_error(env: Path) -> None:
    (env / "proj" / ".aix" / "config.yaml").write_text("execution: {max_parallel: 0}\n")
    code, out = run(env, "agent", "list")
    assert code == 2 and "execution.max_parallel" in out


def test_gate_command_shape(env: Path) -> None:
    """The M2 gate pipes `aix agent list --json` into a python check on ids."""
    ids = list(agents(env))
    assert {"claude", "codex", "fake"} <= set(ids), os.linesep + str(ids)
