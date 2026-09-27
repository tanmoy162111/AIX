"""Plugin manifest schema (PLAYBOOK §25)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from aix.plugins.manifest import PluginManifest, PluginType


def manifest(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "acme-lint",
        "version": "1.2.3",
        "type": "check",
        "capabilities": ["lint"],
        "permissions": ["read_workspace"],
        "requires_aix": ">=0.1,<0.3",
        "entrypoint": "acme.checks:run",
    }
    return base | over


def test_a_complete_manifest_validates() -> None:
    m = PluginManifest.model_validate(manifest())
    assert m.type is PluginType.CHECK and m.version == "1.2.3"
    assert m.capabilities == ["lint"] and m.entrypoint == "acme.checks:run"


def test_every_plugin_type_of_the_spec_exists() -> None:
    assert {t.value for t in PluginType} == {
        "adapter",
        "tool",
        "skill",
        "check",
        "decision_provider",
        "exporter",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "Not Valid"),
        ("id", ""),
        ("version", "1.2"),
        ("version", "v1.2.3"),
        ("type", "wizard"),
        ("requires_aix", "newer than a while"),
        ("requires_aix", ""),
        ("entrypoint", "no_colon"),
        ("entrypoint", "pkg.mod:"),
        ("entrypoint", "pkg mod:attr"),
    ],
)
def test_invalid_fields_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(manifest(**{field: value}))


def test_unknown_fields_and_missing_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(manifest(surprise=1))
    bad = manifest()
    del bad["entrypoint"]
    with pytest.raises(ValidationError):
        PluginManifest.model_validate(bad)


def test_prerelease_and_build_versions_are_semver() -> None:
    assert PluginManifest.model_validate(manifest(version="2.0.0-rc.1+build.5")).version


def test_supports_checks_a_version_against_the_range() -> None:
    m = PluginManifest.model_validate(manifest(requires_aix=">=0.1,<0.3"))
    assert m.supports("0.1.0") and m.supports("0.2.9")
    assert not m.supports("0.3.0") and not m.supports("0.0.9")
    assert m.supports("0.2.0rc1")  # pre-releases of a supported line are fine
