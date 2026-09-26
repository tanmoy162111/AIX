"""Baseline comparison (PLAYBOOK §17.4): do not punish agents for a repo's existing red checks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import Field

from aix.domain.base import DomainModel
from aix.domain.enums import CheckKind
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check
from aix.verification.checks import run_command_check
from aix.verification.commands import ResolvedCommand
from aix.verification.gate import CommandGate

_COUNT_KEY: Final[dict[CheckKind, str]] = {
    CheckKind.TESTS: "tests_failed",
    CheckKind.LINT: "lint_errors",
    CheckKind.TYPECHECK: "type_errors",
}


class Baseline(DomainModel):
    """Check results on the untouched run branch, stored as an artifact (M7)."""

    checks: dict[CheckKind, Check] = Field(default_factory=dict[CheckKind, Check])

    def get(self, kind: CheckKind) -> Check | None:
        """The baseline check of ``kind`` if one was run."""
        return self.checks.get(kind)


async def run_baseline(
    workspace: Path,
    commands: Mapping[CheckKind, ResolvedCommand],
    kinds: Sequence[CheckKind],
    *,
    allow: list[str],
    timeout_s: float,
    network: str = "deny",
    out_dir: Path | None = None,
    command_gate: CommandGate | None = None,
) -> Baseline:
    """Run the command checks in ``kinds`` once on an untouched workspace.

    A ``command_gate`` may refuse a command (``tool_risk``); that kind is then recorded as
    ``error``, which carries no baseline information (§17.4).
    """
    checks: dict[CheckKind, Check] = {}
    for kind in kinds:
        cmd = commands.get(kind)
        if cmd is not None and command_gate is not None:
            refusal = await command_gate(list(cmd.argv))
            if refusal is not None:
                checks[kind] = Check(
                    id=new_id(IdPrefix.CHECK),
                    kind=kind,
                    status="error",
                    required=True,
                    summary=f"command refused: {refusal}",
                    command=list(cmd.argv),
                )
                continue
        res = await run_command_check(
            kind,
            commands.get(kind),
            workspace,
            required=True,
            allow=allow,
            timeout_s=timeout_s,
            network=network,
            out_dir=(out_dir / kind.value) if out_dir else None,
        )
        checks[kind] = res.check
    return Baseline(checks=checks)


def apply_baseline(current: Check, baseline: Check | None) -> Check:
    """Downgrade a failure that already existed at baseline to ``pre_existing``.

    Contract: only a ``failed`` check is ever changed, and only when the baseline of the same
    kind also ``failed``. Where the kind has a failure count (tests, lint, typecheck) it stays
    ``failed`` if the count increased, with the new and pre-existing numbers in the summary;
    otherwise (or for kinds without a count, e.g. build) it becomes ``warning`` with summary
    ``pre_existing: ...`` and metric ``pre_existing = 1``. A baseline that errored or was skipped
    carries no information.
    """
    if baseline is None or current.status != "failed" or baseline.status != "failed":
        return current
    key = _COUNT_KEY.get(current.kind)
    now, before = (current.metrics.get(key), baseline.metrics.get(key)) if key else (None, None)
    if now is not None and before is not None and now > before:
        new = int(now - before)
        return current.model_copy(
            update={"summary": f"{current.summary} ({new} new, {int(before)} pre-existing)"}
        )
    return current.model_copy(
        update={
            "status": "warning",
            "severity": "info",
            "summary": f"pre_existing: {current.summary}",
            "metrics": {**current.metrics, "pre_existing": 1.0},
        }
    )
