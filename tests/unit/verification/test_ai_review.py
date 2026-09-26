from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

import repos
from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.protocol import AgentPermissions
from aix.config.schema import RoutingConfig
from aix.core.planner.agent import make_adapter_runner
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.agents import AgentSpec
from aix.domain.enums import Capability
from aix.domain.enums import CheckKind as K
from aix.domain.errors import NoEligibleAgent
from aix.domain.ids import IdPrefix, new_id
from aix.domain.verification import Check
from aix.verification.ai_review import (
    ReviewParseError,
    parse_review_findings,
    pick_reviewer,
    render_review_prompt,
    run_ai_review,
)

pytestmark = pytest.mark.anyio
SNAPSHOT = Path(__file__).resolve().parents[2] / "fixtures" / "prompts" / "review.txt"


def block(*findings: dict[str, JsonValue]) -> str:
    return "Looks mostly fine.\n```json\n" + json.dumps({"findings": list(findings)}) + "\n```\n"


def finding(sev: str = "high", conf: float = 0.9, **kw: JsonValue) -> dict[str, JsonValue]:
    return {"severity": sev, "file": "app/auth.py", "line": 10, "title": "Missing check",
            "detail": "token is not verified", "confidence": conf, **kw}  # fmt: skip


# ---- parsing ---------------------------------------------------------------------------------


def test_parse_takes_the_last_fenced_block() -> None:
    text = block(finding("low")) + "\nsecond thoughts\n" + block(finding("critical"))
    out = parse_review_findings(text)
    assert [f.severity for f in out] == ["critical"]
    assert out[0].file == "app/auth.py" and out[0].line == 10


def test_parse_accepts_an_empty_list_and_bare_json() -> None:
    assert parse_review_findings(block()) == []
    assert parse_review_findings('done {"findings": []}') == []


@pytest.mark.parametrize(
    "text",
    [
        "no json at all",
        "```json\n{not json}\n```",
        '```json\n{"other": 1}\n```',
        '```json\n{"findings": {}}\n```',
        block({"severity": "nasty", "title": "t", "detail": "d", "confidence": 0.5}),
        block({"severity": "high", "title": "t", "detail": "d", "confidence": 1.5}),
        block({"severity": "high", "detail": "d", "confidence": 0.5}),
    ],
)
def test_unparseable_findings_are_rejected(text: str) -> None:
    with pytest.raises(ReviewParseError):
        parse_review_findings(text)


# ---- prompt ----------------------------------------------------------------------------------


def _c(kind: K, status: str, summary: str) -> Check:
    return Check(
        id=new_id(IdPrefix.CHECK),
        kind=kind,
        status=status,  # type: ignore[arg-type]
        required=True,
        summary=summary,
    )


CHECKS = [
    _c(K.TESTS, "failed", "1 of 3 tests failed (t::bad)"),
    _c(K.LINT, "passed", "lint passed"),
]
DIFF = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"


def test_prompt_snapshot_and_sections() -> None:
    text = render_review_prompt("Add JWT authentication", DIFF, CHECKS)
    if not SNAPSHOT.exists():  # pragma: no cover - first run only
        SNAPSHOT.write_text(text)
    assert text == SNAPSHOT.read_text()
    for section in ("TASK TYPE: review", "ROLE", "TASK GOAL", "DIFF", "CHECK RESULTS", "OUTPUT"):
        assert section in text
    assert "tests: failed" in text and DIFF.strip() in text and '"findings"' in text
    assert not any(v in text.lower() for v in ("claude", "codex", "gemini", "opencode"))


def test_huge_diffs_are_truncated_with_a_note() -> None:
    text = render_review_prompt("g", "+x\n" * 100_000, [], max_diff_chars=1000)
    assert "diff truncated" in text and len(text) < 6000


# ---- the check -------------------------------------------------------------------------------


async def review(reply: str | Exception, **kw: object) -> Check:
    async def runner(_prompt: str) -> str:
        if isinstance(reply, Exception):
            raise reply
        return reply

    res = await run_ai_review("goal", DIFF, [], runner, reviewer_id="fake-rev", **kw)  # type: ignore[arg-type]
    return res


async def test_high_confident_finding_fails_and_names_the_reviewer() -> None:
    c = await review(block(finding("high", 0.8)))
    assert c.kind is K.AI_REVIEW and c.status == "failed" and c.severity == "high"
    assert "fake-rev" in c.summary and "Missing check" in c.summary
    assert c.metrics == {"findings_high": 1.0} and not c.required


@pytest.mark.parametrize(
    ("findings", "status"),
    [
        ([], "passed"),
        ([finding("high", 0.59)], "warning"),  # below the confidence bar
        ([finding("medium", 0.99)], "warning"),
        ([finding("critical", 0.6)], "failed"),
    ],
)
async def test_status_rules(findings: list[dict[str, JsonValue]], status: str) -> None:
    assert (await review(block(*findings))).status == status


async def test_unparseable_reply_and_runner_errors_are_errors_not_passes() -> None:
    bad = await review("I think it is fine")
    assert bad.status == "error" and "unparseable" in bad.summary
    boom = await review(RuntimeError("agent died"))
    assert boom.status == "error" and "agent died" in boom.summary


async def test_required_flag_is_passed_through() -> None:
    assert (await review(block(), required=True)).required


# ---- independent reviewer --------------------------------------------------------------------


def spec(agent_id: str, review_prior: float, health: str = "ready") -> AgentSpec:
    return AgentSpec(
        id=agent_id, name=agent_id, kind="local", health=health,  # type: ignore[arg-type]
        capabilities={Capability.REVIEW: review_prior},
    )  # fmt: skip


def test_pick_reviewer_prefers_someone_other_than_the_author() -> None:
    agents = [spec("author", 0.95), spec("other", 0.7)]
    assert pick_reviewer(agents, {"author"}, RoutingConfig()) == "other"
    assert pick_reviewer(agents, set(), RoutingConfig()) == "author"


def test_pick_reviewer_falls_back_to_the_author_when_alone_and_fails_when_none() -> None:
    assert pick_reviewer([spec("author", 0.9)], {"author"}, RoutingConfig()) == "author"
    with pytest.raises(NoEligibleAgent):
        pick_reviewer([spec("down", 0.9, "unavailable")], set(), RoutingConfig())


# ---- end to end with a fake reviewer agent ---------------------------------------------------


async def test_review_through_a_fake_agent_in_a_read_only_worktree(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "proj")
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    step = FakeStep(claim="Two things.", emit_findings=[finding("high", 0.9)])
    adapter = FakeAdapter(
        "fake-rev", scripts=[FakeScript(match=FakeMatch(task_type="review"), attempts=[step])]
    )
    runner = make_adapter_runner(adapter, wm, run_id, timeout_s=30)
    c = await run_ai_review("Add JWT", DIFF, CHECKS, runner, reviewer_id="fake-rev")
    assert c.status == "failed" and c.metrics == {"findings_high": 1.0}
    assert list((repo / ".aix" / "worktrees").iterdir()) == []
    assert AgentPermissions(read_only=True).read_only
