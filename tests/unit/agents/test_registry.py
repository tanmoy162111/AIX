from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

import factories as f
from aix.agents.manifest import AdapterManifest
from aix.agents.protocol import AgentEvent, AgentHandle, AgentOutcome, AgentRequest
from aix.agents.registry import AdapterEntry, AdapterRegistry
from aix.config.schema import AixConfig
from aix.domain.agents import AgentSpec
from aix.domain.errors import ConfigError

pytestmark = pytest.mark.anyio


class Stub:
    def __init__(self, id: str, health: str = "ready", boom: bool = False) -> None:
        self.id = id
        self._health = health
        self._boom = boom

    async def probe(self) -> AgentSpec:
        if self._boom:
            raise RuntimeError("probe exploded")
        return f.agent_spec(id=self.id, health=self._health, health_reason=None)

    async def start(self, req: AgentRequest) -> AgentHandle:
        raise NotImplementedError

    def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        raise NotImplementedError

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        raise NotImplementedError

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None: ...

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        raise NotImplementedError


def entry(id: str, **kw: object) -> AdapterEntry:
    m = AdapterManifest(id=id, name=id.title(), kind="cli", binary=id, cost_class="low")
    return AdapterEntry(manifest=m, adapter=Stub(id, **kw))  # type: ignore[arg-type]


def registry(enabled: list[str], *entries: AdapterEntry) -> AdapterRegistry:
    cfg = AixConfig.model_validate({"agents": {"enabled": enabled}})
    reg = AdapterRegistry(cfg, builtin_ids=())
    for e in entries:
        reg.register(e)
    return reg


def test_ids_get_and_unknown() -> None:
    reg = registry(["a"], entry("a"), entry("b"))
    assert reg.ids() == ["a", "b"]
    assert reg.get("a").id == "a"
    assert reg.manifest("b").name == "B"
    with pytest.raises(ConfigError, match="unknown agent 'zzz'"):
        reg.get("zzz")


def test_duplicate_registration_is_an_error() -> None:
    reg = registry(["a"], entry("a"))
    with pytest.raises(ConfigError, match="already registered"):
        reg.register(entry("a"))


def test_is_enabled_follows_config_and_fake_is_always_on() -> None:
    reg = registry(["a"], entry("a"), entry("b"), entry("fake"))
    assert reg.is_enabled("a") and not reg.is_enabled("b") and reg.is_enabled("fake")


async def test_probe_marks_unlisted_agents_disabled() -> None:
    reg = registry(["a"], entry("a"), entry("b"))
    a, b = await reg.probe("a"), await reg.probe("b")
    assert a.health == "ready"
    assert b.health == "disabled" and "disabled" in (b.health_reason or "")


async def test_probe_failure_becomes_unavailable_not_a_crash() -> None:
    reg = registry(["a"], entry("a", boom=True))
    spec = await reg.probe("a")
    assert spec.health == "unavailable" and "probe exploded" in (spec.health_reason or "")


async def test_probe_all_lists_everything_even_unavailable() -> None:
    reg = registry(["a", "b"], entry("a"), entry("b", health="unavailable"))
    specs = await reg.probe_all()
    assert [(s.id, s.health) for s in specs] == [("a", "ready"), ("b", "unavailable")]


async def test_eligible_returns_only_ready_or_degraded_enabled() -> None:
    reg = registry(
        ["a", "b", "c"],
        entry("a"),
        entry("b", health="degraded"),
        entry("c", health="unavailable"),
        entry("d"),
    )
    assert [s.id for s in await reg.eligible()] == ["a", "b"]


def test_builtin_manifests_declare_matching_ids(tmp_path: Path) -> None:
    from aix.agents.registry import BUILTIN_IDS

    reg = AdapterRegistry(AixConfig())
    assert set(reg.ids()) == set(BUILTIN_IDS)
    for id in reg.ids():
        assert reg.manifest(id).id == id
        assert reg.get(id).id == id
