from __future__ import annotations

from pathlib import Path

import yaml

from aix.cli.init import config_template
from aix.config.edit import set_agent_enabled
from aix.config.loader import load_config


def _proj(tmp_path: Path, text: str | None = None) -> tuple[Path, Path]:
    (tmp_path / ".aix").mkdir()
    cfg = tmp_path / ".aix" / "config.yaml"
    if text is not None:
        cfg.write_text(text)
    return tmp_path, tmp_path / "no-user.yaml"


def test_disable_starts_from_the_resolved_list_and_keeps_template_comments(tmp_path: Path) -> None:
    proj, user = _proj(tmp_path, config_template())
    result = set_agent_enabled(proj, "codex", False, user_config=user)
    assert result == ["claude", "gemini", "opencode"]
    text = (proj / ".aix" / "config.yaml").read_text()
    assert text.startswith("# aix project configuration")  # comments preserved
    assert load_config(proj, user_config=user).config.agents.enabled == result


def test_enable_appends_once_and_is_idempotent(tmp_path: Path) -> None:
    proj, user = _proj(tmp_path, "agents: {enabled: [claude]}\nexecution: {max_parallel: 2}\n")
    assert set_agent_enabled(proj, "codex", True, user_config=user) == ["claude", "codex"]
    assert set_agent_enabled(proj, "codex", True, user_config=user) == ["claude", "codex"]
    doc = yaml.safe_load((proj / ".aix" / "config.yaml").read_text())
    assert doc["execution"] == {"max_parallel": 2}
    assert doc["agents"]["enabled"] == ["claude", "codex"]


def test_disabling_an_absent_agent_is_a_noop(tmp_path: Path) -> None:
    proj, user = _proj(tmp_path, "agents: {enabled: [claude]}\n")
    assert set_agent_enabled(proj, "gemini", False, user_config=user) == ["claude"]


def test_creates_the_file_if_missing(tmp_path: Path) -> None:
    proj, user = _proj(tmp_path)
    set_agent_enabled(proj, "codex", False, user_config=user)
    assert (proj / ".aix" / "config.yaml").is_file()
