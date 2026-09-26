"""Run report model and deterministic renderers (PLAYBOOK §21.4, §23.3)."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import jinja2
from pydantic import Field

from aix.domain.base import DomainModel
from aix.domain.enums import CheckKind, TaskStatus
from aix.store.db import EventStore

_SECURITY: Final = (CheckKind.SECRETS, CheckKind.SECURITY_SAST, CheckKind.DEPS)


class TaskRow(DomainModel):
    id: str
    title: str
    type: str
    status: str
    agent: str | None
    attempts: int
    failure: str | None


class RunReport(DomainModel):
    """Everything the human summary shows, derived only from recorded events."""

    run_id: str
    goal: str
    status: str
    branch: str
    tasks: list[TaskRow]
    retries: int
    tests_passed: int
    tests_total: int
    security: list[str]
    review: list[str]
    decisions_total: int
    decisions_by_provider: dict[str, int]
    final_decision: str | None
    agents: list[str]
    cost_usd: float | None
    cost_estimated: bool
    duration_s: float | None
    artifacts: list[str] = Field(default_factory=list[str])
    bundle: str | None = None
    """Project-relative path of the exported evidence bundle, if one was written."""

    @property
    def failure(self) -> str | None:
        """The first recorded task failure class, if any."""
        return next((t.failure for t in self.tasks if t.failure), None)

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.status == TaskStatus.COMPLETED.value)


async def build_report(
    store: EventStore, run_id: str, *, artifact_names: Sequence[str] = ()
) -> RunReport:
    """Collect a :class:`RunReport` for ``run_id`` from the store. Pure function of the events."""
    run = await store.get_run(run_id)
    if run is None:
        raise LookupError(f"unknown run {run_id}")
    tasks, rows = await store.get_tasks(run_id), []
    tests_passed = tests_total = total_attempts = 0
    security: list[str] = []
    review: list[str] = []
    per_agent: dict[str, Counter[str]] = {}
    costs: list[float] = []
    estimated = False
    for task in tasks:
        attempts = await store.get_attempts(task.id)
        total_attempts += len(attempts)
        last = attempts[-1] if attempts else None
        rows.append(
            TaskRow(
                id=task.id, title=task.title, type=task.type.value, status=task.status.value,
                agent=last.agent_id if last else None, attempts=len(attempts), failure=None,
            )
        )  # fmt: skip
        for a in attempts:
            per_agent.setdefault(a.agent_id, Counter())[task.type.value] += 1
            if (res := await store.get_result(a.id)) is not None and res.usage.cost_usd is not None:
                costs.append(res.usage.cost_usd)
                estimated = estimated or res.usage.estimated
        if last is not None:
            for c in await store.get_checks(last.id):
                if c.kind is CheckKind.TESTS:
                    tests_passed += int(c.metrics.get("tests_passed", 0))
                    tests_total += int(c.metrics.get("tests_total", 0))
                elif c.kind in _SECURITY:
                    security.append(f"{c.kind.value}: {c.status}")
                elif c.kind is CheckKind.AI_REVIEW:
                    review.append(f"{c.status}: {c.summary}")
            if (result := await store.get_result(last.id)) is not None and result.failure:
                rows[-1] = rows[-1].model_copy(update={"failure": result.failure.value})
    decisions = await store.get_decisions(run_id)
    duration = (run.finished_at - run.created_at).total_seconds() if run.finished_at else None
    agents = [
        f"{aid} ({', '.join(f'{t} x{n}' if n > 1 else t for t, n in sorted(c.items()))})"
        for aid, c in sorted(per_agent.items())
    ]
    return RunReport(
        run_id=run_id, goal=run.goal, status=run.status.value, branch=f"aix/run/{run_id}",
        tasks=rows, retries=total_attempts - sum(1 for r in rows if r.attempts),
        tests_passed=tests_passed, tests_total=tests_total,
        security=sorted(set(security)), review=review,
        decisions_total=len(decisions),
        decisions_by_provider=dict(sorted(Counter(d.provider for d in decisions).items())),
        final_decision=decisions[-1].outcome.value if decisions else None,
        agents=agents, cost_usd=round(sum(costs), 6) if costs else None,
        cost_estimated=estimated, duration_s=duration, artifacts=list(artifact_names),
    )  # fmt: skip


def _env(html: bool) -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.PackageLoader("aix.artifacts", "templates"),
        undefined=jinja2.StrictUndefined, keep_trailing_newline=True, trim_blocks=True,
        lstrip_blocks=True, autoescape=html,
    )  # fmt: skip


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    m, s = divmod(round(seconds), 60)
    return f"{m}m{s:02d}s" if m else f"{s}s"


def _context(r: RunReport) -> dict[str, object]:
    by_provider = ", ".join(f"{k}: {v}" for k, v in r.decisions_by_provider.items())
    cost = (
        "n/a"
        if r.cost_usd is None
        else f"${r.cost_usd:.2f}" + (" (estimated)" if r.cost_estimated else "")
    )
    return {
        "r": r,
        "retries": f"{r.retries} retr{'y' if r.retries == 1 else 'ies'}",
        "tests": f"{r.tests_passed}/{r.tests_total} passed" if r.tests_total else "none run",
        "security": ", ".join(r.security) or "none run",
        "decisions": f"{r.decisions_total} recorded"
        + (f"  ({by_provider})" if by_provider else ""),
        "cost": cost,
        "duration": _fmt_duration(r.duration_s),
    }


def render_markdown(report: RunReport) -> str:
    """``report.md``: the §23.3 summary plus task table. Deterministic for a fixed report."""
    return _env(False).get_template("report.md.j2").render(**_context(report))


def render_html(report: RunReport) -> str:
    """``report.html``: self-contained page with inline CSS, no scripts, escaped content."""
    return _env(True).get_template("report.html.j2").render(**_context(report))


def render_summary(report: RunReport) -> str:
    """The §23.3 stdout block."""
    return _env(False).get_template("summary.txt.j2").render(**_context(report))


async def artifact_names(store: EventStore, root: Path, run_id: str) -> list[str]:
    """Names listed in the run's manifest artifact (empty when none was written)."""
    from aix.artifacts.store import ObjectStore
    from aix.domain.enums import ArtifactType

    manifests = [a for a in await store.get_artifacts(run_id) if a.type is ArtifactType.MANIFEST]
    if not manifests:
        return []
    blob = await ObjectStore(root / ".aix" / "artifacts" / "objects").get(manifests[-1].sha256)
    return [str(e["name"]) for e in json.loads(blob)["artifacts"]]
