"""Check and VerificationReport (§6, §17)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Self

from pydantic import Field, model_validator

from aix.domain.base import DomainModel, Sha256
from aix.domain.enums import CheckKind
from aix.domain.ids import ArtifactId, AttemptId, CheckId

CheckStatus = Literal["passed", "failed", "warning", "skipped", "error"]
Severity = Literal["info", "low", "medium", "high", "critical"]
Overall = Literal["passed", "failed", "warning", "incomplete"]


class ArtifactRef(DomainModel):
    """Reference to a stored artifact (log, junit xml, sarif ...)."""

    id: ArtifactId
    sha256: Sha256 | None = None


class Check(DomainModel):
    """One executed verification step and its evidence."""

    id: CheckId
    kind: CheckKind
    status: CheckStatus
    severity: Severity = "info"
    required: bool
    summary: str
    """Machine-generated and factual."""
    metrics: dict[str, float] = Field(default_factory=dict)
    evidence: list[ArtifactRef] = Field(default_factory=list[ArtifactRef])
    command: list[str] | None = None
    duration_ms: int = Field(default=0, ge=0)


def compute_overall(checks: Sequence[Check]) -> Overall:
    """Aggregate check statuses into a report status (§6).

    Any required check failed/error -> ``failed``; else any required skipped -> ``incomplete``;
    else any warning or optional failure/error -> ``warning``; else ``passed``. A required check
    that could not run is never treated as success (§17.2).
    """
    if any(c.required and c.status in ("failed", "error") for c in checks):
        return "failed"
    if any(c.required and c.status == "skipped" for c in checks):
        return "incomplete"
    if any(
        c.status == "warning" or (not c.required and c.status in ("failed", "error"))
        for c in checks
    ):
        return "warning"
    return "passed"


class VerificationReport(DomainModel):
    """All checks for an attempt; ``overall`` must equal ``compute_overall(checks)``."""

    attempt_id: AttemptId
    checks: list[Check]
    overall: Overall

    @model_validator(mode="after")
    def _overall_matches_rule(self) -> Self:
        expected = compute_overall(self.checks)
        if self.overall != expected:
            raise ValueError(f"overall is {self.overall!r} but the checks imply {expected!r}")
        return self
