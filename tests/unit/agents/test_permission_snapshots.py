"""Policy permissions -> native CLI flags, per adapter, snapshot-tested (M8.1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aix.agents.adapters.claude.argv import build_argv as claude
from aix.agents.adapters.codex.argv import build_argv as codex
from aix.agents.adapters.gemini.argv import build_argv as gemini
from aix.agents.adapters.opencode.argv import build_argv as opencode
from aix.agents.protocol import AgentPermissions, AgentRequest

SNAP = Path(__file__).resolve().parents[2] / "fixtures" / "permissions"
ADAPTERS = {"claude": claude, "codex": codex, "gemini": gemini, "opencode": opencode}
CASES: dict[str, AgentPermissions] = {
    "read_only": AgentPermissions(read_only=True),
    "workspace": AgentPermissions(),
    "scoped": AgentPermissions(write_scope=["app/**", "tests/*.py"]),
    "network_allow": AgentPermissions(network="allow"),
    "network_deny": AgentPermissions(network="deny"),
    "read_only_tools": AgentPermissions(read_only=True, allowed_tools=["Read", "Grep"]),
}


def request(perms: AgentPermissions) -> AgentRequest:
    return AgentRequest(
        attempt_id="att_01ARZ3NDEKTSV4RRFFQ69G5FAV", workspace=Path("/ws"), prompt="p",
        timeout_s=60, permissions=perms,
    )  # fmt: skip


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_permission_translation_snapshot(name: str) -> None:
    doc = {case: ADAPTERS[name](name, request(p)) for case, p in CASES.items()}
    text = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    snap = SNAP / f"{name}.json"
    if not snap.exists():  # pragma: no cover - first run only
        snap.write_text(text)
    assert text == snap.read_text()


@pytest.mark.parametrize("name", sorted(ADAPTERS))
def test_no_adapter_ever_disables_its_own_safety(name: str) -> None:
    banned = ("--dangerously", "bypass", "--yolo", "danger-full-access", "-y")
    for perms in CASES.values():
        argv = ADAPTERS[name](name, request(perms))
        assert not [a for a in argv if any(b in a for b in banned)], (name, argv)


def test_read_only_is_native_where_the_cli_supports_it() -> None:
    ro = request(CASES["read_only"])
    assert "read-only" in codex("codex", ro)
    assert "plan" in gemini("gemini", ro) and "plan" in opencode("opencode", ro)
