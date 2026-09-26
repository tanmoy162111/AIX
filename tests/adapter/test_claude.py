"""Claude adapter end to end against a fake `claude` binary that replays recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

from aix.agents.adapters.claude import ClaudeAdapter
from aix.agents.protocol import AgentAdapter, AgentOutcome, AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id
from binaries import make_replay_binary

pytestmark = pytest.mark.anyio

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "claude"
HELP_OK = (
    "--output-format --verbose --permission-mode --allowedTools --disallowedTools --model --resume"
)


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.bin = tmp / "bin"
        self.log = tmp / "log"
        self.ws = tmp / "ws"
        self.ws.mkdir()
        self.tmp = tmp

    def install(self, recording: str | None = None, **kw: object) -> ClaudeAdapter:
        rec = REC / recording if recording else None
        make_replay_binary(
            self.bin,
            "claude",
            recording=rec,
            log_dir=self.log,
            help_text=HELP_OK,
            **kw,  # type: ignore[arg-type]
        )
        return ClaudeAdapter(
            env_source={
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(self.tmp),
                "ANTHROPIC_API_KEY": "sk-test-not-real",
                "UNRELATED_SECRET": "leak-me",
            }
        )

    def req(self, **kw: object) -> AgentRequest:
        data: dict[str, object] = {
            "attempt_id": new_id(IdPrefix.ATTEMPT),
            "workspace": self.ws,
            "prompt": "TASK TYPE: implement\nadd hello",
            "timeout_s": 30,
            "permissions": AgentPermissions(),
        }
        data.update(kw)
        return AgentRequest.model_validate(data)

    def argv(self) -> list[str]:
        return json.loads((self.log / "argv.json").read_text())


async def run(adapter: AgentAdapter, req: AgentRequest) -> tuple[list[str], AgentOutcome]:
    h = await adapter.start(req)
    kinds = [e.kind async for e in adapter.events(h)]
    return kinds, await adapter.wait(h)


async def test_successful_execution(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("success.jsonl"), rig.req())
    assert kinds == ["started", "text", "tool_call", "tool_result", "text", "usage", "finished"]
    assert out.status == "completed" and out.exit_code == 0 and out.failure is None
    assert out.claim == "Done. The endpoint is added and all tests pass."
    assert out.usage.cost_usd == 0.0421 and out.usage.output_tokens == 92
    assert out.session_ref == "11111111-2222-3333-4444-555555555555"


async def test_invocation_argv_stdin_cwd_and_env(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await run(rig.install("success.jsonl"), rig.req(model="opus"))
    argv = rig.argv()
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert "--model" in argv and "opus" in argv and "--permission-mode" in argv
    assert (rig.log / "stdin.txt").read_text() == "TASK TYPE: implement\nadd hello"
    assert (rig.log / "cwd.txt").read_text() == str(rig.ws.resolve())
    env = set(json.loads((rig.log / "env.json").read_text()))
    assert "ANTHROPIC_API_KEY" in env and "PATH" in env
    assert "UNRELATED_SECRET" not in env


async def test_wait_without_reading_events_gives_the_same_outcome(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("success.jsonl")
    h = await a.start(rig.req())
    out = await a.wait(h)
    assert out.status == "completed" and out.usage.cost_usd == 0.0421


async def test_raw_stream_is_captured(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    path = tmp_path / "runs" / "a.stream.jsonl"
    await run(rig.install("success.jsonl"), rig.req(stream_path=path))
    assert path.read_text() == (REC / "success.jsonl").read_text()


async def test_read_only_request_translates_permissions(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(
        rig.install("read_only.jsonl"), rig.req(permissions=AgentPermissions(read_only=True))
    )
    assert out.status == "completed"
    argv = rig.argv()
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "Edit,Write,NotebookEdit,Bash" in argv


@pytest.mark.parametrize(
    ("recording", "exit_code", "expected"),
    [
        ("auth_failure.jsonl", 1, FailureClass.AUTH_FAILURE),
        ("rate_limit.jsonl", 1, FailureClass.RATE_LIMITED),
        ("max_turns.jsonl", 1, FailureClass.AGENT_FAILURE),
        ("budget.jsonl", 1, FailureClass.BUDGET_EXCEEDED),
    ],
)
async def test_failure_recordings_are_classified(
    tmp_path: Path, recording: str, exit_code: int, expected: FailureClass
) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install(recording, exit_code=exit_code), rig.req())
    assert out.status == "failed" and out.failure is expected
    assert out.exit_code == exit_code


async def test_error_result_with_exit_zero_is_still_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("auth_failure.jsonl", exit_code=0), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AUTH_FAILURE


async def test_nonzero_exit_without_result_uses_stderr_classification(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install(None, exit_code=1, stderr="Error: 429 rate limit exceeded\n")
    _, out = await run(a, rig.req())
    assert out.status == "failed" and out.failure is FailureClass.RATE_LIMITED
    assert "rate limit" in out.stderr_tail


async def test_malformed_stream_lines_do_not_break_the_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("malformed.jsonl"), rig.req())
    assert kinds.count("error") == 3
    assert out.status == "completed" and out.claim == "ok after garbage"


async def test_stream_without_result_is_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("no_result.jsonl"), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AGENT_FAILURE
    assert "without a result" in out.stderr_tail


async def test_timeout_is_classified(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("no_result.jsonl", sleep_s=30)
    _, out = await run(a, rig.req(timeout_s=1))
    assert out.status == "timeout" and out.failure is FailureClass.TIMEOUT


async def test_cancellation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("no_result.jsonl", sleep_s=30)
    async with anyio.create_task_group() as tg:  # opened before start() so scopes nest (LIFO)
        h = await a.start(rig.req())

        async def cancel_soon() -> None:
            await anyio.sleep(0.5)
            await a.cancel(h, grace_s=1)

        tg.start_soon(cancel_soon)
        out = await a.wait(h)
    assert out.status == "cancelled" and out.failure is None


async def test_resume_passes_the_session_id(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("success.jsonl")
    h = await a.resume("sess-42", rig.req())
    await a.wait(h)
    argv = rig.argv()
    assert argv[argv.index("--resume") + 1] == "sess-42"


async def test_missing_binary_start_raises_typed_error(tmp_path: Path) -> None:
    from aix.domain.errors import ToolFailure

    rig = Rig(tmp_path)
    a = ClaudeAdapter(env_source={"PATH": str(tmp_path / "empty"), "HOME": str(tmp_path)})
    with pytest.raises(ToolFailure, match="not found"):
        await a.start(rig.req())


# ---- probe ------------------------------------------------------------------------------


async def test_probe_ready_reports_version_and_manifest_capabilities(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = await rig.install(None, version="2.1.283 (Claude Code)").probe()
    assert spec.id == "claude" and spec.health == "ready" and spec.version == "2.1.283"
    assert spec.capabilities and spec.supports.sessions and spec.supports.model_select
    assert spec.cost_class == "high"


async def test_probe_degraded_when_required_flags_are_missing(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    make_replay_binary(
        rig.bin, "claude", version="2.0.0", help_text="--output-format --verbose --model"
    )
    a = ClaudeAdapter(env_source={"PATH": f"{rig.bin}{os.pathsep}{os.environ['PATH']}"})
    spec = await a.probe()
    assert spec.health == "degraded"
    assert "--permission-mode" in (spec.health_reason or "")


async def test_probe_unavailable_when_binary_missing(tmp_path: Path) -> None:
    a = ClaudeAdapter(env_source={"PATH": str(tmp_path / "empty")})
    spec = await a.probe()
    assert spec.health == "unavailable" and "not found" in (spec.health_reason or "")


def test_satisfies_protocol() -> None:
    assert isinstance(ClaudeAdapter(), AgentAdapter)
