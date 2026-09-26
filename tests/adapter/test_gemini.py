"""Gemini adapter end to end against a fake `gemini` binary that replays recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

from aix.agents.adapters.gemini import GeminiAdapter
from aix.agents.protocol import AgentAdapter, AgentOutcome, AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id
from binaries import make_replay_binary

pytestmark = pytest.mark.anyio

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "gemini"
HELP_OK = "Usage: gemini --prompt --output-format --approval-mode --model --skip-trust --resume"


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.bin, self.log = tmp, tmp / "bin", tmp / "log"
        self.ws = tmp / "ws"
        self.ws.mkdir()

    def install(self, recording: str | None = None, **kw: object) -> GeminiAdapter:
        make_replay_binary(
            self.bin,
            "gemini",
            recording=REC / recording if recording else None,
            log_dir=self.log,
            help_text=HELP_OK,
            **kw,  # type: ignore[arg-type]
        )
        return GeminiAdapter(
            env_source={
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(self.tmp),
                "GEMINI_API_KEY": "test-key-not-real",
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
    assert kinds[0] == "started" and kinds[-2:] == ["usage", "finished"]
    assert out.status == "completed" and out.failure is None
    assert out.claim == "Added GET /hello. All tests pass."
    assert out.usage.input_tokens == 15000 and out.usage.cost_usd is None
    assert out.session_ref == "b1c2d3e4-1111-4222-8333-444455556666"


async def test_invocation_argv_stdin_and_env(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await run(rig.install("success.jsonl"), rig.req(model="gemini-x"))
    argv = rig.argv()
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert argv[argv.index("--approval-mode") + 1] == "auto_edit"
    assert "--skip-trust" in argv and argv[argv.index("--model") + 1] == "gemini-x"
    assert "add hello" not in " ".join(argv)  # the task is on stdin, never in argv
    assert (rig.log / "stdin.txt").read_text() == "TASK TYPE: implement\nadd hello"
    env = set(json.loads((rig.log / "env.json").read_text()))
    assert "GEMINI_API_KEY" in env and "UNRELATED_SECRET" not in env


async def test_read_only_uses_plan_mode(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(
        rig.install("read_only.jsonl"), rig.req(permissions=AgentPermissions(read_only=True))
    )
    assert out.status == "completed" and out.claim == "No issues found."
    assert rig.argv()[rig.argv().index("--approval-mode") + 1] == "plan"


async def test_raw_stream_is_captured_and_wait_alone_works(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    path = tmp_path / "runs" / "a.stream.jsonl"
    a = rig.install("success.jsonl")
    h = await a.start(rig.req(stream_path=path))
    out = await a.wait(h)
    assert out.status == "completed"
    assert path.read_text() == (REC / "success.jsonl").read_text()


@pytest.mark.parametrize(
    ("recording", "expected"),
    [
        ("auth_failure.jsonl", FailureClass.AUTH_FAILURE),
        ("rate_limit.jsonl", FailureClass.RATE_LIMITED),
        ("network.jsonl", FailureClass.NETWORK_FAILURE),
        ("context.jsonl", FailureClass.CONTEXT_FAILURE),
    ],
)
async def test_failure_recordings_are_classified(
    tmp_path: Path, recording: str, expected: FailureClass
) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install(recording, exit_code=1), rig.req())
    assert out.status == "failed" and out.failure is expected


async def test_error_result_with_exit_zero_is_still_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("rate_limit.jsonl", exit_code=0), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.RATE_LIMITED


async def test_exit_code_41_is_an_auth_failure_even_without_a_stream(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install(None, exit_code=41, stderr="Login required\n"), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AUTH_FAILURE


async def test_malformed_stream_lines_do_not_break_the_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("malformed.jsonl"), rig.req())
    assert kinds.count("error") == 3 and out.status == "completed"
    assert out.claim == "ok after garbage"


async def test_stream_without_a_result_is_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("no_result.jsonl"), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AGENT_FAILURE
    assert "result" in out.stderr_tail


async def test_timeout_and_cancellation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("no_result.jsonl", sleep_s=30)
    _, out = await run(a, rig.req(timeout_s=1))
    assert out.status == "timeout" and out.failure is FailureClass.TIMEOUT

    async with anyio.create_task_group() as tg:
        h = await a.start(rig.req())

        async def cancel_soon() -> None:
            await anyio.sleep(0.5)
            await a.cancel(h, grace_s=1)

        tg.start_soon(cancel_soon)
        out2 = await a.wait(h)
    assert out2.status == "cancelled" and out2.failure is None


async def test_resume_passes_the_session_id(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("success.jsonl")
    h = await a.resume("sess-42", rig.req())
    await a.wait(h)
    argv = rig.argv()
    assert argv[argv.index("--resume") + 1] == "sess-42"


async def test_probe_ready_degraded_and_unavailable(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = await rig.install(None, version="0.55.1").probe()
    assert spec.health == "ready" and spec.version == "0.55.1"
    assert spec.supports.sessions and spec.cost_class == "low"

    make_replay_binary(rig.bin, "gemini", version="0.1.0", help_text="--prompt --model")
    degraded = await GeminiAdapter(
        env_source={"PATH": f"{rig.bin}{os.pathsep}{os.environ['PATH']}"}
    ).probe()
    assert degraded.health == "degraded"
    assert "--approval-mode" in (degraded.health_reason or "")

    gone = await GeminiAdapter(env_source={"PATH": str(tmp_path / "empty")}).probe()
    assert gone.health == "unavailable"


def test_satisfies_protocol() -> None:
    assert isinstance(GeminiAdapter(), AgentAdapter)
