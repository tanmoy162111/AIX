"""Fast, deterministic verification setup for orchestrator tests (no reliance on machine tools)."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from aix.config.schema import (
    AixConfig,
    ExecutionConfig,
    PlannerConfig,
    SecurityConfig,
    VerificationConfig,
)
from aix.domain.enums import CheckKind as K

PY = Path(sys.executable).name
PASS = [sys.executable, "-c", "pass"]
COMMANDS = {K.BUILD: PASS, K.TESTS: PASS, K.LINT: PASS, K.TYPECHECK: PASS}


def fast_config(**kw: object) -> AixConfig:
    """Config whose build/tests/lint/typecheck are no-op commands that always pass."""
    kw.setdefault("planner", PlannerConfig(provider="template"))
    kw.setdefault("execution", ExecutionConfig(escalation_ladder=[]))  # failures end, not ask
    return AixConfig(
        verification=VerificationConfig(commands=dict(COMMANDS)),
        security=SecurityConfig(shell_allow=[PY, "git"]),
        **kw,  # type: ignore[arg-type]
    )


def write_fast_config(project: Path, **commands: list[str]) -> None:
    """Write the same setup into ``<project>/.aix/config.yaml`` (for CLI tests)."""
    cmds = {k.value: v for k, v in COMMANDS.items()} | commands
    doc = {
        "verification": {"commands": cmds},
        "security": {"shell_allow": [PY, "git"]},
        "execution": {"escalation_ladder": []},
    }
    (project / ".aix" / "config.yaml").write_text(yaml.safe_dump(doc))
