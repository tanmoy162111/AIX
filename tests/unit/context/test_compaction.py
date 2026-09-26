from __future__ import annotations

import pytest

from aix.core.context.compaction import compact_context
from aix.domain.context import Handoff

pytestmark = pytest.mark.anyio


def handoff(n: int, files: list[str], issues: list[str] | None = None) -> Handoff:
    return Handoff(
        task_id=f"task_01ARZ3NDEKTSV4RRFFQ69G5F{n:02d}"[:31].ljust(31, "A"),
        summary=f"Task: t{n}\nChanges: {len(files)} files (+1 -0)\nChecks: tests=passed\n\n"
        f"AGENT CLAIM (unverified):\n> trust me {n}",
        files_changed=files,
        open_issues=issues or [],
    )


HANDOFFS = [
    handoff(1, ["a.py", "b.py"], ["lint: warning - x"]),
    handoff(2, ["b.py", "c.py"], ["lint: warning - x", "deps: skipped - y"]),
]


async def test_below_budget_no_compaction() -> None:
    assert await compact_context(HANDOFFS, budget_tokens=10_000) is None


async def test_compacts_when_over_budget_deterministically() -> None:
    kw = {"decisions": ["dec_1 task_completion:accept"], "failures": ["f1", "f2", "f1"]}
    a = await compact_context(HANDOFFS, budget_tokens=10, **kw)  # type: ignore[arg-type]
    b = await compact_context(HANDOFFS, budget_tokens=10, **kw)  # type: ignore[arg-type]
    assert a is not None and a == b
    r = a.result
    assert r.important_files == ["b.py", "a.py", "c.py"]  # b.py touched twice
    assert r.open_questions == ["lint: warning - x", "deps: skipped - y"]
    assert r.known_failures == ["f1", "f2"] and r.decisions == ["dec_1 task_completion:accept"]
    assert "trust me" not in r.summary and "Task: t1" in r.summary
    assert len(a.before_sha256) == 64 and a.before_sha256 != a.after_sha256


async def test_summarizer_replaces_summary_and_falls_back_on_error() -> None:
    async def good(digest: str) -> str:
        assert "Task: t1" in digest
        return "  Two tasks done.  "

    async def bad(_: str) -> str:
        raise RuntimeError("agent down")

    ok = await compact_context(HANDOFFS, budget_tokens=10, summarizer=good)
    fallback = await compact_context(HANDOFFS, budget_tokens=10, summarizer=bad)
    assert ok is not None and ok.result.summary == "Two tasks done."
    assert fallback is not None and "Task: t1" in fallback.result.summary
