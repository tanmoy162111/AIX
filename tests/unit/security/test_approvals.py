from __future__ import annotations

import stat
from pathlib import Path

import pytest

from aix.security.approvals import (
    ApprovalRefused,
    confirm_tty,
    ensure_token,
    guard_agent_context,
    short_id,
    token_path,
    verify_token,
)

APV = "apv_01M3EYCHDGW3NJ3CJ942SYBEFJ"


@pytest.fixture(autouse=True)
def _xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("AIX_AGENT_CONTEXT", raising=False)


def test_token_is_created_once_with_mode_0600() -> None:
    first = ensure_token()
    assert len(first) >= 32 and ensure_token() == first
    mode = stat.S_IMODE(token_path().stat().st_mode)
    assert mode == 0o600
    token_path().chmod(0o644)
    ensure_token()  # re-tightens loose permissions
    assert stat.S_IMODE(token_path().stat().st_mode) == 0o600


def test_verify_token_accepts_only_the_real_one() -> None:
    real = ensure_token()
    verify_token(real)
    for bad in (None, "", "nope", real + "x"):
        with pytest.raises(ApprovalRefused):
            verify_token(bad)


def test_agent_context_is_refused() -> None:
    guard_agent_context({})
    with pytest.raises(ApprovalRefused, match="agent context"):
        guard_agent_context({"AIX_AGENT_CONTEXT": "1"})


def test_agent_context_from_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIX_AGENT_CONTEXT", "1")
    with pytest.raises(ApprovalRefused):
        guard_agent_context()


def test_tty_confirmation_requires_a_terminal_and_the_typed_short_id() -> None:
    sid = short_id(APV)
    assert sid == APV[-6:].lower()
    confirm_tty(APV, isatty=True, prompt=lambda _q: sid)
    confirm_tty(APV, isatty=True, prompt=lambda _q: f"  {sid.upper()} ")
    with pytest.raises(ApprovalRefused, match="did not match"):
        confirm_tty(APV, isatty=True, prompt=lambda _q: "wrong")
    with pytest.raises(ApprovalRefused, match="terminal"):
        confirm_tty(APV, isatty=False, prompt=lambda _q: sid)


def test_agent_process_env_never_carries_the_token_or_config_dir() -> None:
    from aix.agents.env import build_agent_env

    env = build_agent_env([], {}, source={"PATH": "/bin", "XDG_CONFIG_HOME": "/x", "HOME": "/h"})
    assert "XDG_CONFIG_HOME" not in env and env["AIX_AGENT_CONTEXT"] == "1"
