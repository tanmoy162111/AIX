from __future__ import annotations

import json
import subprocess
from pathlib import Path

import anyio
import pytest
import yaml

from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import FakeScript, FakeStep, load_scripts
from aix.agents.fakes import make_fake_entry
from aix.agents.protocol import AgentAdapter, AgentEvent, AgentPermissions, AgentRequest
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AixConfig
from aix.domain.enums import Capability, FailureClass
from aix.domain.errors import UnsupportedError
from aix.domain.ids import IdPrefix, new_id

pytestmark = pytest.mark.anyio

SPEC_SCRIPT = """
match: { task_type: implement }
attempts:
  - events: [{kind: text, data: {text: "Implementing JWT"}}]
    claim: "Done. All tests pass."
    exit_code: 0
  - claim: "Fixed failing test."
    exit_code: 0
"""

PATCH = """\
diff --git a/hello.txt b/hello.txt
new file mode 100644
index 0000000..ce01362
--- /dev/null
+++ b/hello.txt
@@ -0,0 +1 @@
+hello
"""


def req(ws: Path, prompt: str = "TASK TYPE: implement\ndo it", **kw: object) -> AgentRequest:
    data: dict[str, object] = {
        "attempt_id": new_id(IdPrefix.ATTEMPT),
        "workspace": ws,
        "prompt": prompt,
        "timeout_s": 30,
        "permissions": AgentPermissions(),
    }
    data.update(kw)
    return AgentRequest.model_validate(data)


async def run(adapter: FakeAdapter, r: AgentRequest) -> tuple[list[AgentEvent], object]:
    h = await adapter.start(r)
    events = [e async for e in adapter.events(h)]
    return events, await adapter.wait(h)


def script(**step: object) -> FakeScript:
    return FakeScript(attempts=[FakeStep.model_validate(step)])


def test_spec_script_parses() -> None:
    s = FakeScript.model_validate(yaml.safe_load(SPEC_SCRIPT))
    assert s.match.task_type == "implement" and len(s.attempts) == 2
    assert s.attempts[0].events[0].kind == "text"


def test_load_scripts_from_file_and_directory(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(SPEC_SCRIPT)
    (tmp_path / "b.yaml").write_text("attempts: [{claim: x}]\n")
    assert len(load_scripts(tmp_path / "a.yaml")) == 1
    assert len(load_scripts(tmp_path)) == 2


def test_satisfies_protocol() -> None:
    assert isinstance(FakeAdapter("fake"), AgentAdapter)


async def test_default_behavior_without_scripts(tmp_path: Path) -> None:
    events, out = await run(FakeAdapter("fake"), req(tmp_path))
    assert [e.kind for e in events] == ["started", "finished"]
    assert out.exit_code == 0 and out.status == "completed" and out.claim  # type: ignore[attr-defined]


async def test_events_claim_and_usage(tmp_path: Path) -> None:
    s = script(
        events=[{"kind": "text", "data": {"text": "hi"}}],
        claim="all good",
        usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.02},
    )
    events, out = await run(FakeAdapter("fake", scripts=[s]), req(tmp_path))
    assert [e.kind for e in events] == ["started", "text", "usage", "finished"]
    assert out.claim == "all good"  # type: ignore[attr-defined]
    assert out.usage.cost_usd == 0.02  # type: ignore[attr-defined]


async def test_attempts_advance_and_last_repeats(tmp_path: Path) -> None:
    s = FakeScript.model_validate(yaml.safe_load(SPEC_SCRIPT))
    a = FakeAdapter("fake", scripts=[s])
    claims = [(await run(a, req(tmp_path)))[1].claim for _ in range(3)]  # type: ignore[attr-defined]
    assert claims == ["Done. All tests pass.", "Fixed failing test.", "Fixed failing test."]


async def test_matching_by_task_type_and_prompt(tmp_path: Path) -> None:
    impl = FakeScript.model_validate(
        {"match": {"task_type": "implement"}, "attempts": [{"claim": "I"}]}
    )
    rev = FakeScript.model_validate(
        {"match": {"task_type": "review", "prompt_contains": "auth"}, "attempts": [{"claim": "R"}]}
    )
    a = FakeAdapter("fake", scripts=[impl, rev])
    assert (await run(a, req(tmp_path, "TASK TYPE: implement\nx")))[1].claim == "I"  # type: ignore[attr-defined]
    assert (await run(a, req(tmp_path, "TASK TYPE: review\nadd auth")))[1].claim == "R"  # type: ignore[attr-defined]
    other = (await run(a, req(tmp_path, "TASK TYPE: review\nunrelated")))[1]
    assert other.claim not in ("I", "R")  # type: ignore[attr-defined]


async def test_apply_patch_relative_to_script_dir(tmp_path: Path) -> None:
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "p.diff").write_text(PATCH)
    ws = tmp_path / "ws"
    ws.mkdir()
    s = script(apply_patch="patches/p.diff")
    a = FakeAdapter("fake", scripts=[s], base_dir=tmp_path)
    _, out = await run(a, req(ws))
    assert (ws / "hello.txt").read_text() == "hello\n"
    assert out.status == "completed"  # type: ignore[attr-defined]


async def test_bad_patch_fails_the_attempt(tmp_path: Path) -> None:
    (tmp_path / "bad.diff").write_text("not a patch")
    a = FakeAdapter("fake", scripts=[script(apply_patch="bad.diff")], base_dir=tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    _, out = await run(a, req(ws))
    assert out.status == "failed" and out.failure is FailureClass.TOOL_FAILURE  # type: ignore[attr-defined]


async def test_write_files_and_outside_scope(tmp_path: Path) -> None:
    s = script(write_files={"src/a.py": "x = 1\n"}, write_outside_scope=["deploy/prod.yaml"])
    await run(FakeAdapter("fake", scripts=[s]), req(tmp_path))
    assert (tmp_path / "src" / "a.py").read_text() == "x = 1\n"
    assert (tmp_path / "deploy" / "prod.yaml").exists()


async def test_write_files_cannot_escape_the_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    a = FakeAdapter("fake", scripts=[script(write_files={"../evil.txt": "x"})])
    _, out = await run(a, req(ws))
    assert not (tmp_path / "evil.txt").exists()
    assert out.status == "failed"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("HTTP 429 rate limit exceeded", FailureClass.RATE_LIMITED),
        ("401 Unauthorized: invalid api key", FailureClass.AUTH_FAILURE),
        ("segfault", FailureClass.AGENT_FAILURE),
    ],
)
async def test_nonzero_exit_classification(
    tmp_path: Path, stderr: str, expected: FailureClass
) -> None:
    a = FakeAdapter("fake", scripts=[script(exit_code=1, stderr=stderr)])
    _, out = await run(a, req(tmp_path))
    assert out.status == "failed" and out.failure is expected  # type: ignore[attr-defined]
    assert stderr in out.stderr_tail  # type: ignore[attr-defined]


async def test_explicit_failure_overrides_classification(tmp_path: Path) -> None:
    a = FakeAdapter("fake", scripts=[script(exit_code=2, failure="network_failure")])
    _, out = await run(a, req(tmp_path))
    assert out.failure is FailureClass.NETWORK_FAILURE  # type: ignore[attr-defined]


async def test_sleep_beyond_timeout_is_a_timeout(tmp_path: Path) -> None:
    a = FakeAdapter("fake", scripts=[script(sleep_s=5)])
    _, out = await run(a, req(tmp_path, timeout_s=1))
    assert out.status == "timeout" and out.failure is FailureClass.TIMEOUT  # type: ignore[attr-defined]


async def test_cancel_interrupts_sleep(tmp_path: Path) -> None:
    a = FakeAdapter("fake", scripts=[script(sleep_s=30)])
    h = await a.start(req(tmp_path))
    async with anyio.create_task_group() as tg:
        tg.start_soon(lambda: a.cancel(h))  # type: ignore[arg-type,return-value]
        out = await a.wait(h)
    assert out.status == "cancelled"


async def test_emit_findings_appends_fenced_json(tmp_path: Path) -> None:
    findings = [
        {
            "severity": "high",
            "file": "a.py",
            "line": 3,
            "title": "t",
            "detail": "d",
            "confidence": 0.9,
        }
    ]
    a = FakeAdapter("fake", scripts=[script(claim="Review done.", emit_findings=findings)])
    _, out = await run(a, req(tmp_path))
    claim: str = out.claim  # type: ignore[attr-defined]
    block = claim.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert json.loads(block) == {"findings": findings}


async def test_planner_output_is_the_claim(tmp_path: Path) -> None:
    a = FakeAdapter("fake", scripts=[script(planner_output={"tasks": []})])
    _, out = await run(a, req(tmp_path))
    assert json.loads(out.claim) == {"tasks": []}  # type: ignore[attr-defined]


async def test_resume_is_unsupported(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedError):
        await FakeAdapter("fake").resume("sess", req(tmp_path))


async def test_probe_reports_configured_health_and_capabilities() -> None:
    a = FakeAdapter(
        "fake-b",
        capabilities={Capability.IMPLEMENT: 0.9},
        health="unavailable",
        health_reason="scripted outage",
    )
    spec = await a.probe()
    assert spec.id == "fake-b" and spec.health == "unavailable"
    assert spec.health_reason == "scripted outage"
    assert spec.capabilities == {Capability.IMPLEMENT: 0.9}


async def test_builtin_fake_and_extra_fakes_in_registry() -> None:
    reg = AdapterRegistry(AixConfig())
    assert "fake" in reg.ids()
    reg.register(make_fake_entry("fake-a", capabilities={Capability.DESIGN: 0.9}))
    reg.register(make_fake_entry("fake-reviewer", capabilities={Capability.REVIEW: 0.9}))
    specs = {s.id: s for s in await reg.probe_all()}
    assert {"fake", "fake-a", "fake-reviewer"} <= set(specs)
    fakes = [s for i, s in specs.items() if i.startswith("fake")]
    assert all(s.health == "ready" for s in fakes)  # fake and fake-* are always enabled


def test_git_available_for_patch_tests() -> None:
    assert subprocess.run(["git", "--version"], capture_output=True).returncode == 0


async def test_fake_agent_applies_a_real_fixture_patch(tmp_path: Path) -> None:
    import repos

    repo = repos.materialize_sample_py(tmp_path / "r")
    a = FakeAdapter(
        "fake",
        scripts=[script(apply_patch="patches/hello.diff")],
        base_dir=repos.FIXTURES / "agent_scripts",
    )
    _, out = await run(a, req(repo))
    assert out.status == "completed"  # type: ignore[attr-defined]
    assert (repo / "tests" / "test_hello.py").exists()
    assert repos.run_pytest(repo).returncode == 0
