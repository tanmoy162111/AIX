from __future__ import annotations

from pathlib import Path

from aix.core.context.facts import ProjectFacts
from aix.core.context.prompt import render_task_prompt
from aix.domain.context import Handoff
from aix.domain.enums import Capability, TaskType
from aix.domain.enums import CheckKind as K
from aix.domain.tasks import RepoFacts, Task, VerificationSpec

SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "task.txt"
RUN = "run_01ARZ3NDEKTSV4RRFFQ69G5FAV"
TASK_ID = "task_01ARZ3NDEKTSV4RRFFQ69G5FAV"
DEP_ID = "task_01ARZ3NDEKTSV4RRFFQ69G5FAW"


def make_task(scope: list[str] | None = None) -> Task:
    return Task(
        id=TASK_ID, run_id=RUN, title="Add auth", goal="Add JWT authentication to app/auth.py",
        type=TaskType.IMPLEMENT, required_capabilities=[Capability.IMPLEMENT],
        file_scope=["app/**"] if scope is None else scope,
        verification=VerificationSpec(required=[K.BUILD, K.TESTS], optional=[K.LINT]),
    )  # fmt: skip


HANDOFF = Handoff(
    task_id=DEP_ID,
    summary="Task: Add models\nChanges: 1 files (+5 -0)\nChecks: tests=passed\n\n"
    "AGENT CLAIM (unverified):\n> All done.",
    files_changed=["app/models.py"],
    open_issues=["lint: warning - 2 warnings"],
)
FACTS = ProjectFacts(
    head="a" * 40,
    repo=RepoFacts(languages=["python"], test_commands=["pytest -q"]),
    conventions={"CLAUDE.md": "Use make check."},
)


def full() -> str:
    return render_task_prompt(
        make_task(), handoffs=[HANDOFF], failure_notes=["attempt 1: tests failed (3)"],
        facts=FACTS, skill_instructions="Write tests first.",
    )  # fmt: skip


def test_snapshot_and_sections_in_spec_order() -> None:
    text = full()
    if not SNAPSHOT.exists():  # pragma: no cover - first run only
        SNAPSHOT.write_text(text)
    assert text == SNAPSHOT.read_text()
    order = [
        "TASK TYPE: implement", "ROLE", "TASK GOAL", "CONSTRAINTS", "FILE SCOPE",
        "DEPENDENCY HANDOFFS", "PREVIOUS ATTEMPT FAILURES", "PROJECT FACTS",
        "SKILL INSTRUCTIONS", "DEFINITION OF DONE", "DO NOT",
    ]  # fmt: skip
    idx = [text.index(s) for s in order]
    assert idx == sorted(idx) and text.startswith("TASK TYPE: implement\n")
    assert "not accepted as evidence" in text
    assert not any(v in text.lower() for v in ("claude ", "codex", "gemini", "opencode"))


def test_optional_sections_disappear_when_empty() -> None:
    text = render_task_prompt(make_task())
    for s in ("DEPENDENCY HANDOFFS", "PREVIOUS ATTEMPT FAILURES", "PROJECT FACTS", "SKILL INSTR"):
        assert s not in text
    assert "build, tests, lint" in text


def test_read_only_task() -> None:
    text = render_task_prompt(make_task([]))
    assert "read-only" in text and "You may change only" not in text


def test_budget_drops_facts_first_but_keeps_goal() -> None:
    text = render_task_prompt(make_task(), handoffs=[HANDOFF], facts=FACTS, budget_tokens=30)
    assert "Add JWT authentication" in text and "PROJECT FACTS" not in text


def test_deterministic() -> None:
    assert full() == full()
