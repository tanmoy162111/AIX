"""Adapters as plugins: built-ins and external ones share one loading path (PLAYBOOK §25, §10)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aix.agents.registry import BUILTIN_IDS, AdapterRegistry
from aix.config.schema import AgentsConfig, AixConfig
from aix.domain.errors import ConfigError
from plugin_env import install_sample_plugin

pytestmark = pytest.mark.anyio


@pytest.fixture
def sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_sample_plugin(tmp_path / "site", monkeypatch)


def config(*enabled: str) -> AixConfig:
    return AixConfig(agents=AgentsConfig(enabled=list(enabled)))


@pytest.mark.usefixtures("sample")
async def test_an_installed_adapter_plugin_is_discovered_and_usable() -> None:
    reg = AdapterRegistry(config("sample-agent"))
    assert "sample-agent" in reg.ids()
    assert reg.manifest("sample-agent").name == "Sample plugin agent"
    spec = await reg.probe("sample-agent")
    assert spec.health == "ready" and spec.capabilities
    assert "sample-agent" in [s.id for s in await reg.eligible()]


@pytest.mark.usefixtures("sample")
async def test_installing_a_plugin_does_not_enable_it() -> None:
    reg = AdapterRegistry(config("claude"))
    assert not reg.is_enabled("sample-agent")
    spec = await reg.probe("sample-agent")
    assert spec.health == "disabled"
    assert "sample-agent" not in [s.id for s in await reg.eligible()]


@pytest.mark.usefixtures("sample")
def test_broken_plugins_are_skipped_and_reported_without_hiding_the_good_one() -> None:
    reg = AdapterRegistry(config("sample-agent"))
    assert "future-agent" not in reg.ids() and "ghost-agent" not in reg.ids()
    problems = {r.id: r.error or "" for r in reg.plugin_records() if not r.ok}
    assert set(problems) == {"future-agent", "ghost-agent"}
    assert "requires aix >=99.0" in problems["future-agent"]
    assert "does_not_exist" in problems["ghost-agent"]
    assert next(r for r in reg.plugin_records() if r.id == "sample-agent").ok


def test_builtin_adapters_are_registered_through_the_plugin_path() -> None:
    reg = AdapterRegistry(config())
    records = {r.id: r for r in reg.plugin_records()}
    assert set(BUILTIN_IDS) <= set(records)
    assert all(records[i].source == "builtin" and records[i].ok for i in BUILTIN_IDS)
    manifest = records["claude"].manifest
    assert manifest is not None and manifest.type.value == "adapter"
    assert manifest.entrypoint == "aix.agents.adapters.claude:create"
    assert "implement" in manifest.capabilities


@pytest.mark.usefixtures("sample")
def test_hermetic_registries_do_not_discover_plugins() -> None:
    reg = AdapterRegistry(config(), builtin_ids=())
    assert reg.ids() == [] and reg.plugin_records() == []
    opt_in = AdapterRegistry(config(), builtin_ids=(), discover_plugins=True)
    assert opt_in.ids() == ["sample-agent"]


class EP:
    def __init__(self, name: str, manifest: dict[str, Any]) -> None:
        self.name, self.value, self._m = name, "x:y", manifest

    def load(self) -> dict[str, Any]:
        return self._m


def test_a_plugin_cannot_replace_a_builtin() -> None:
    manifest = {
        "id": "claude",
        "version": "9.9.9",
        "type": "adapter",
        "requires_aix": ">=0.1",
        "entrypoint": "aix_sample_plugin.adapter:create",
    }
    reg = AdapterRegistry(
        config(),
        plugin_entry_points=lambda *, group: (
            [EP("claude", manifest)] if group == "aix.adapters" else []
        ),
    )
    claude = next(r for r in reg.plugin_records() if r.id == "claude" and r.source != "builtin")
    assert not claude.ok and "duplicate" in (claude.error or "")
    assert reg.manifest("claude").name != "Sample plugin agent"


def test_a_plugin_whose_manifest_yaml_names_another_id_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_sample_plugin(tmp_path / "site", monkeypatch)
    site = tmp_path / "site" / "aix_sample_plugin" / "adapter" / "manifest.yaml"
    site.write_text(site.read_text().replace("id: sample-agent", "id: impostor"))
    reg = AdapterRegistry(config("sample-agent"))
    bad = next(r for r in reg.plugin_records() if r.id == "sample-agent")
    assert not bad.ok and "impostor" in (bad.error or "")
    assert "sample-agent" not in reg.ids()


def test_a_broken_builtin_is_still_a_packaging_error() -> None:
    with pytest.raises(ConfigError, match="cannot load built-in adapter"):
        AdapterRegistry(config(), builtin_ids=("no_such_adapter",))
