"""YAML script format for the fake agent (PLAYBOOK §27.1)."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import Field, JsonValue

from aix.agents.protocol import EventKind
from aix.domain.base import DomainModel
from aix.domain.enums import FailureClass
from aix.domain.execution import Usage


class FakeEvent(DomainModel):
    kind: EventKind
    data: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])


class FakeMatch(DomainModel):
    """Which requests a script answers; task type comes from a ``TASK TYPE: <x>`` prompt line."""

    task_type: str | None = None
    prompt_contains: str | None = None


class FakeStep(DomainModel):
    """One scripted attempt."""

    events: list[FakeEvent] = Field(default_factory=list[FakeEvent])
    apply_patch: str | None = None
    """Unified diff path, relative to the adapter's ``base_dir``; applied with ``git apply``."""
    write_files: dict[str, str] = Field(default_factory=dict[str, str])
    write_outside_scope: list[str] = Field(default_factory=list[str])
    run_commands: list[list[str]] = Field(default_factory=list[list[str]])
    """Commands the scripted "agent" runs in its workspace with the real agent environment
    (used by security tests: an agent attempting ``aix approve`` and the like)."""
    claim: str | None = "Done."
    exit_code: int = 0
    stderr: str = ""
    sleep_s: float = Field(default=0.0, ge=0)
    usage: Usage | None = None
    emit_findings: list[dict[str, JsonValue]] | None = None
    planner_output: JsonValue = None
    failure: FailureClass | None = None


class FakeScript(DomainModel):
    """Attempts answered in order for matching requests; the last one repeats."""

    match: FakeMatch = Field(default_factory=FakeMatch)
    attempts: list[FakeStep] = Field(min_length=1)


def load_scripts(path: Path) -> list[FakeScript]:
    """Load one YAML script file, or every ``*.yaml`` in a directory (sorted)."""
    files = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    return [FakeScript.model_validate(yaml.safe_load(f.read_text(encoding="utf-8"))) for f in files]
