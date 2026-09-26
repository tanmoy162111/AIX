from __future__ import annotations

from aix.agents.env import BASE_ENV_KEYS, build_agent_env

MARK = {"AIX_AGENT_CONTEXT": "1"}


def test_only_base_and_allowlisted_variables_pass() -> None:
    source = {
        "PATH": "/bin",
        "HOME": "/h",
        "LANG": "C",
        "SECRET_TOKEN": "s",
        "ANTHROPIC_API_KEY": "k",
        "RANDOM": "r",
    }
    env = build_agent_env(["ANTHROPIC_API_KEY"], {}, source=source)
    assert env == {"PATH": "/bin", "HOME": "/h", "LANG": "C", "ANTHROPIC_API_KEY": "k", **MARK}


def test_request_env_is_added_and_wins() -> None:
    env = build_agent_env([], {"FOO": "1", "PATH": "/custom"}, source={"PATH": "/bin"})
    assert env == {"PATH": "/custom", "FOO": "1", **MARK}


def test_missing_allowlisted_variables_are_skipped() -> None:
    assert build_agent_env(["NOPE"], {}, source={"PATH": "/bin"}) == {"PATH": "/bin", **MARK}


def test_base_keys_are_minimal() -> None:
    assert set(BASE_ENV_KEYS) == {"PATH", "HOME", "LANG", "LC_ALL", "TERM"}


def test_agent_context_marker_is_always_set_and_cannot_be_overridden() -> None:
    env = build_agent_env([], {"AIX_AGENT_CONTEXT": ""}, source={})
    assert env["AIX_AGENT_CONTEXT"] == "1"
