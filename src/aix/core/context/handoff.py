"""Handoff builder (PLAYBOOK §15.2): deterministic facts plus a labeled agent claim."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from aix.domain.context import Handoff
from aix.domain.execution import DiffSummary
from aix.domain.ids import ArtifactId, DecisionId, TaskId
from aix.domain.verification import Check
from aix.security.redact import redact_secrets

CLAIM_LABEL: Final = "AGENT CLAIM (unverified)"
CLAIM_MAX_CHARS: Final = 600
MAX_FILES: Final = 50


def _claim_block(claim: str | None) -> str | None:
    """Quote the claim line by line so it cannot pose as part of the factual summary."""
    text = redact_secrets((claim or "").strip())
    if not text:
        return None
    if len(text) > CLAIM_MAX_CHARS:
        text = text[:CLAIM_MAX_CHARS] + " [truncated]"
    quoted = "\n".join(f"> {line}" for line in text.splitlines())
    return f"{CLAIM_LABEL}:\n{quoted}"


def build_handoff(
    *,
    task_id: TaskId,
    title: str,
    diff: DiffSummary,
    checks: Sequence[Check],
    claim: str | None,
    decisions: Sequence[DecisionId] = (),
    artifacts: Sequence[ArtifactId] = (),
) -> Handoff:
    """Build the handoff for an accepted task.

    Contract: ``summary`` lists the diff stat and each check's status (control-plane facts);
    the agent claim, if any, is redacted, truncated to ``CLAIM_MAX_CHARS`` and appended as a
    quoted block under ``CLAIM_LABEL``. ``open_issues`` names every check that did not pass, so
    dependents see what is still unverified. Output is a pure function of the inputs.
    """
    lines = [
        f"Task: {title}",
        f"Changes: {diff.files_changed} files (+{diff.lines_added} -{diff.lines_removed})",
    ]
    if checks:
        lines.append("Checks: " + ", ".join(f"{c.kind.value}={c.status}" for c in checks))
    else:
        lines.append("Checks: none recorded")
    block = _claim_block(claim)
    if block:
        lines += ["", block]
    issues = [f"{c.kind.value}: {c.status} - {c.summary}" for c in checks if c.status != "passed"]
    return Handoff(
        task_id=task_id,
        summary="\n".join(lines),
        files_changed=sorted(diff.paths)[:MAX_FILES],
        decisions=list(decisions),
        open_issues=issues,
        artifacts=list(artifacts),
    )
