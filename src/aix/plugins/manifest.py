"""Plugin manifest (PLAYBOOK §25)."""

from __future__ import annotations

from enum import StrEnum

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import Field, field_validator

from aix.domain.base import DomainModel

_SEMVER = (
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)
_DOTTED = r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*"
_ENTRYPOINT = rf"^{_DOTTED}:{_DOTTED}$"


class PluginType(StrEnum):
    """What a plugin contributes; each type has its own entry-point group."""

    ADAPTER = "adapter"
    TOOL = "tool"
    SKILL = "skill"
    CHECK = "check"
    DECISION_PROVIDER = "decision_provider"
    EXPORTER = "exporter"


class PluginManifest(DomainModel):
    """Static description of a plugin, validated before any plugin code beyond it is imported."""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]*$")
    version: str = Field(pattern=_SEMVER)
    type: PluginType
    capabilities: list[str] = Field(default_factory=list[str])
    permissions: list[str] = Field(default_factory=list[str])
    """What the plugin says it needs (informational: plugins run in-process, see ADR-0031)."""
    requires_aix: str = Field(min_length=1)
    """Range of supported aix versions, comma-separated comparators such as ``>=0.1,<0.3``."""
    entrypoint: str = Field(pattern=_ENTRYPOINT)
    """``package.module:attribute``, imported only when the plugin is activated."""

    @field_validator("requires_aix")
    @classmethod
    def _valid_range(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"not a version range: {exc}") from None
        return value

    def supports(self, aix_version: str) -> bool:
        """Whether ``aix_version`` satisfies ``requires_aix`` (pre-releases of it included)."""
        try:
            version = Version(aix_version)
        except InvalidVersion:
            return False
        return SpecifierSet(self.requires_aix).contains(version, prereleases=True)
