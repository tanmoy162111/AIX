from __future__ import annotations

from pathlib import Path

from aix.artifacts.report import (
    RunReport,
    TaskRow,
    render_html,
    render_markdown,
    render_summary,
)  # fmt: skip

FIX = Path(__file__).resolve().parents[2] / "fixtures" / "reports"


def report(**kw: object) -> RunReport:
    base: dict[str, object] = dict(
        run_id="run_01ARZ3NDEKTSV4RRFFQ69G5FAV", goal="Add JWT <authentication>",
        status="completed", branch="aix/run/run_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        tasks=[
            TaskRow(id="task_1", title="Design", type="design", status="completed",
                    agent="claude", attempts=1, failure=None),
            TaskRow(id="task_2", title="Implement", type="implement", status="completed",
                    agent="codex", attempts=2, failure=None),
        ],
        retries=1, tests_passed=42, tests_total=42, security=["deps: skipped", "secrets: passed"],
        review=["passed: 0 high, 1 medium"], decisions_total=9,
        decisions_by_provider={"jev": 2, "rules": 7}, final_decision="accept",
        agents=["claude (design)", "codex (implement x2)"], cost_usd=1.84, cost_estimated=True,
        duration_s=852.0, artifacts=["plan.json", "patch/task_2.diff", "manifest.json"],
    )  # fmt: skip
    return RunReport.model_validate({**base, **kw})


def check(name: str, text: str) -> None:
    snap = FIX / name
    if not snap.exists():  # pragma: no cover - first run only
        snap.write_text(text)
    assert text == snap.read_text()


def test_summary_snapshot_follows_the_23_3_layout() -> None:
    text = render_summary(report())
    check("summary.txt", text)
    assert text.startswith("RUN run_01ARZ3NDEKTSV4RRFFQ69G5FAV COMPLETED")
    for label in ("Tasks", "Tests", "Security", "Review", "Decisions", "Agents", "Cost", "Next:"):
        assert label in text


def test_markdown_snapshot() -> None:
    check("report.md", render_markdown(report()))


def test_html_snapshot_escapes_and_has_no_scripts() -> None:
    html = render_html(report())
    check("report.html", html)
    assert "&lt;authentication&gt;" in html and "<authentication>" not in html
    assert "<script" not in html


def test_failed_run_has_no_next_step_and_handles_missing_data() -> None:
    text = render_summary(
        report(status="failed", tasks=[], retries=0, tests_total=0, tests_passed=0, security=[],
               review=[], decisions_total=0, decisions_by_provider={}, final_decision=None,
               agents=[], cost_usd=None, duration_s=None, artifacts=[])
    )  # fmt: skip
    assert "Next:" not in text and "Tests        none run" in text and "Cost         n/a" in text
    assert "Review" not in text


def test_rendering_is_deterministic() -> None:
    assert render_markdown(report()) == render_markdown(report())
    assert render_html(report()) == render_html(report())
