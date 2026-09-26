from __future__ import annotations

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
