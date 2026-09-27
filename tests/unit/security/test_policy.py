from __future__ import annotations

import pytest

from aix.config.schema import SecurityConfig
from aix.domain.agents import AgentSpec, AgentSupports
from aix.domain.enums import Capability, TaskType
from aix.domain.tasks import Task
from aix.security.policy import POLICY_VERSION, Policy

RUN = "run_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TASK = "task_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def task(scope: list[str], risk: str = "low") -> Task:
    return Task(
        id=TASK, run_id=RUN, title="t", goal="g", type=TaskType.IMPLEMENT,
        required_capabilities=[Capability.IMPLEMENT], file_scope=scope, risk=risk,  # type: ignore[arg-type]
    )  # fmt: skip


def agent(*enforces: str) -> AgentSpec:
    return AgentSpec(
        id="a", name="a", kind="cli", cost_class="low", capabilities={Capability.IMPLEMENT: 0.9},
        supports=AgentSupports(enforces=list(enforces)),  # type: ignore[arg-type]
        health="ready",
    )  # fmt: skip


def test_hash_is_stable_versioned_and_tracks_config() -> None:
    a, b = Policy(SecurityConfig()), Policy(SecurityConfig())
    assert a.hash == b.hash and len(a.hash) == 64 and POLICY_VERSION == "policy-v2"
    changed = Policy(SecurityConfig(shell_allow=["git"]))
    assert changed.hash != a.hash
    assert Policy(SecurityConfig(sandbox="container")).hash != a.hash


def test_high_risk_needs_native_enforcement_in_local_mode() -> None:
    p = Policy(SecurityConfig())
    scoped, read_only, everything = task(["app/**"], "high"), task([], "high"), task(["**"], "high")
    assert not p.can_run_agent(agent("read_only"), scoped)
    assert "write_scope" in p.can_run_agent(agent("read_only"), scoped).reason
    assert p.can_run_agent(agent("write_scope"), scoped)
    assert not p.can_run_agent(agent("write_scope"), read_only)
    assert p.can_run_agent(agent("read_only"), read_only)
    assert p.can_run_agent(agent(), everything)  # nothing to enforce


def test_low_risk_and_container_mode_are_not_restricted() -> None:
    assert Policy(SecurityConfig()).can_run_agent(agent(), task(["app/**"], "low"))
    container = Policy(SecurityConfig(sandbox="container"))
    assert container.can_run_agent(agent(), task(["app/**"], "high"))


def test_agent_permissions() -> None:
    p = Policy(SecurityConfig())
    assert p.agent_permissions(task([])).read_only
    scoped = p.agent_permissions(task(["app/**"]))
    assert not scoped.read_only and scoped.write_scope == ["app/**"]
    assert p.agent_permissions(task(["**"])).write_scope == []
    assert p.agent_permissions(task(["**"])).network == "provider_default"


def test_can_exec_and_requires_approval() -> None:
    p = Policy(SecurityConfig())
    assert p.can_exec(["pytest", "-q"]) and p.can_exec(["/usr/bin/git", "status"])
    assert not p.can_exec(["curl", "x"]) and not p.can_exec([])
    assert p.requires_approval("push") and not p.requires_approval("read")


def test_can_write() -> None:
    p, t = Policy(SecurityConfig()), task(["app/**"])
    assert p.can_write("app/x.py", t)
    for bad in ("other/x.py", ".aix/aix.db", ".git/config", "../x", "/etc/passwd"):
        assert not p.can_write(bad, t), bad
    assert not p.can_write("a.py", task([]))


WS = "/proj/.aix/worktrees/att_01ARZ3NDEKTSV4RRFFQ69G5FAV"


def test_own_worktree_path_is_not_control_plane_state() -> None:
    """Found live (M10): worktrees sit under `.aix/`, and codex reports absolute paths."""
    p = Policy(SecurityConfig())
    call = f'file_change: [{{"path": "{WS}/app/greeting.py", "kind": "add"}}]'
    assert p.inspect_tool_call("file_change", call, workspace=WS) is None
    assert p.inspect_tool_call("file_change", call) is not None  # without the root it still flags


def test_state_dir_inside_the_worktree_and_outside_it_are_still_flagged() -> None:
    p = Policy(SecurityConfig())
    inside = p.inspect_tool_call("Edit", f"{WS}/.aix/aix.db", workspace=WS)
    assert inside is not None and inside[0] == "control_plane_state"
    outside = p.inspect_tool_call("Edit", "/proj/.aix/aix.db", workspace=WS)
    assert outside is not None and outside[0] == "control_plane_state"


@pytest.mark.parametrize(
    "command",
    [
        "find . -path ./.git -prune -o -path ./.aix -prune -o -type f -print | sort",
        "find . -type f -not -path './.git/*' -not -path './.aix/*' | sort",
        "find . ! -path './.aix/*' -type f",
        "grep -rn TODO --exclude-dir=.aix --exclude-dir=.git .",
        "rg TODO -g '!.aix' .",
    ],
)
def test_commands_that_only_exclude_the_state_dir_are_not_flagged(command: str) -> None:
    """Found live (M10): a reviewer's `find ... -path ./.aix -prune` was called tampering."""
    assert Policy(SecurityConfig()).inspect_tool_call("bash", command, workspace=WS) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat .aix/aix.db",
        "find .aix -type f",
        "ls -la .aix",
        "sqlite3 ./.aix/aix.db .dump",
        "find . -path ./.aix -prune -o -print; cat .aix/aix.db",
        "grep -r token .aix",
    ],
)
def test_commands_that_touch_the_state_dir_are_still_flagged(command: str) -> None:
    hit = Policy(SecurityConfig()).inspect_tool_call("bash", command, workspace=WS)
    assert hit is not None and hit[0] == "control_plane_state"
