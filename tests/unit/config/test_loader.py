from __future__ import annotations

from pathlib import Path

import pytest

from aix.config.loader import load_config, user_config_path
from aix.config.schema import AixConfig
from aix.domain.enums import CheckKind
from aix.domain.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "proj"
    (project / ".aix").mkdir(parents=True)
    user = tmp_path / "home" / "config.yaml"
    return project, user


def test_defaults_match_spec_section_9(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    r = load_config(project, user_config=user)
    c = r.config
    assert c.agents.enabled == ["claude", "codex", "gemini", "opencode"]
    assert c.agents.overrides["codex"].timeout_s == 1800
    assert c.routing.strategy == "capability"
    assert c.routing.prefer_independent_reviewer is True
    assert c.planner.provider == "auto" and c.planner.max_tasks == 12
    assert c.execution.max_parallel == 3 and c.execution.attempt_timeout_s == 1800
    assert c.execution.workspace == "worktree"
    assert c.verification.required_default == [CheckKind.BUILD, CheckKind.TESTS, CheckKind.LINT]
    assert c.decision.provider == "rules"
    assert c.decision.jev.timeout_ms == 3000 and c.decision.jev.on_error == "fallback_rules"
    assert c.decision.thresholds["task_completion.accept_confidence"] == 0.85
    assert c.decision.thresholds["failure_triage.min_confidence"] == 0.6
    assert c.decision.thresholds["tool_risk.deny_if_p_risky_above"] == 0.3
    assert c.budget.max_cost_usd_per_run == 10.0
    assert c.budget.max_attempts_per_run == 20
    assert c.budget.max_wall_seconds_per_run == 7200
    assert c.security.sandbox == "local"
    assert "git" in c.security.shell_allow and "uv" in c.security.shell_allow
    assert "deploy" in c.security.approval_required_for
    assert c.artifacts.bundle is True and c.artifacts.formats == ["json", "md", "html"]
    assert set(r.sources.values()) == {"default"}


def test_layer_precedence_and_source_tracking(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(user, "execution: {max_parallel: 5, attempt_timeout_s: 100}\nplanner: {max_tasks: 8}\n")
    _write(
        project / ".aix" / "config.yaml",
        "execution: {max_parallel: 2}\nbudget: {max_cost_usd_per_run: 3.5}\n",
    )
    r = load_config(
        project,
        user_config=user,
        skill_defaults={"execution": {"max_parallel": 4}, "planner": {"max_tasks": 6}},
        task_overrides={"execution": {"attempt_timeout_s": 60}},
        cli_overrides={"planner": {"max_tasks": 3}},
    )
    c = r.config
    assert c.execution.max_parallel == 4  # skill beats project beats user
    assert c.execution.attempt_timeout_s == 60  # task beats user
    assert c.planner.max_tasks == 3  # cli beats skill and user
    assert c.budget.max_cost_usd_per_run == 3.5
    s = r.sources
    assert s["execution.max_parallel"] == "skill"
    assert s["execution.attempt_timeout_s"] == "task"
    assert s["planner.max_tasks"] == "cli"
    assert s["budget.max_cost_usd_per_run"] == f"project:{project / '.aix' / 'config.yaml'}"
    assert s["routing.strategy"] == "default"


def test_user_layer_is_reported_with_its_path(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(user, "decision: {provider: fake}\n")
    r = load_config(project, user_config=user)
    assert r.config.decision.provider == "fake"
    assert r.sources["decision.provider"] == f"user:{user}"


def test_nested_dicts_merge_and_lists_replace(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(
        project / ".aix" / "config.yaml",
        "decision:\n  thresholds:\n    task_completion.accept_confidence: 0.9\n"
        "agents: {enabled: [codex]}\n",
    )
    r = load_config(project, user_config=user)
    t = r.config.decision.thresholds
    assert t["task_completion.accept_confidence"] == 0.9
    assert t["failure_triage.min_confidence"] == 0.6  # untouched sibling keeps its default
    assert r.config.agents.enabled == ["codex"]  # lists replace
    assert r.sources["agents.enabled"].startswith("project:")
    assert r.sources["decision.thresholds.failure_triage.min_confidence"] == "default"


def test_missing_files_are_fine_and_empty_file_is_fine(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(project / ".aix" / "config.yaml", "")
    assert load_config(project, user_config=user).config == AixConfig()


def test_invalid_yaml_names_the_file(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    bad = _write(project / ".aix" / "config.yaml", "execution: [unclosed\n")
    with pytest.raises(ConfigError, match=str(bad)):
        load_config(project, user_config=user)


def test_top_level_must_be_a_mapping(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(user, "- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(project, user_config=user)


def test_unknown_key_is_rejected_with_its_source(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    cfg = _write(project / ".aix" / "config.yaml", "execution: {max_paralel: 2}\n")
    with pytest.raises(ConfigError) as ei:
        load_config(project, user_config=user)
    msg = str(ei.value)
    assert "execution.max_paralel" in msg and str(cfg) in msg


def test_invalid_value_is_rejected(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(project / ".aix" / "config.yaml", "execution: {max_parallel: 0}\n")
    with pytest.raises(ConfigError, match=r"execution\.max_parallel"):
        load_config(project, user_config=user)
    _write(project / ".aix" / "config.yaml", "decision: {provider: magic}\n")
    with pytest.raises(ConfigError, match=r"decision\.provider"):
        load_config(project, user_config=user)


@pytest.mark.parametrize(
    "text",
    [
        "decision: {jev: {api_key: abc}}\n",
        "TYPESAFE_API_KEY: abc\n",
        "security: {approval_token: abc}\n",
        "agents: {overrides: {codex: {password: x}}}\n",
    ],
)
def test_secrets_in_config_files_are_refused(dirs: tuple[Path, Path], text: str) -> None:
    project, user = dirs
    _write(project / ".aix" / "config.yaml", text)
    with pytest.raises(ConfigError, match="secret"):
        load_config(project, user_config=user)


def test_verification_command_overrides_are_typed(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(project / ".aix" / "config.yaml", "verification: {commands: {tests: [pytest, -q]}}\n")
    r = load_config(project, user_config=user)
    assert r.config.verification.commands == {CheckKind.TESTS: ["pytest", "-q"]}
    _write(project / ".aix" / "config.yaml", "verification: {commands: {nonsense: [x]}}\n")
    with pytest.raises(ConfigError):
        load_config(project, user_config=user)


def test_user_config_path_honours_xdg(tmp_path: Path) -> None:
    assert user_config_path({"XDG_CONFIG_HOME": str(tmp_path)}) == tmp_path / "aix" / "config.yaml"
    assert user_config_path({"HOME": "/h"}) == Path("/h/.config/aix/config.yaml")


def test_rows_lists_every_leaf_with_source(dirs: tuple[Path, Path]) -> None:
    project, user = dirs
    _write(project / ".aix" / "config.yaml", "execution: {max_parallel: 2}\n")
    rows = {k: (v, s) for k, v, s in load_config(project, user_config=user).rows()}
    assert rows["execution.max_parallel"][0] == 2
    assert rows["execution.max_parallel"][1].startswith("project:")
    assert rows["planner.provider"] == ("auto", "default")
    assert "decision.thresholds.task_completion.accept_confidence" in rows
