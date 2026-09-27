"""OpenCode adapter end to end against a fake `opencode` binary that replays recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

from aix.agents.adapters.opencode import OpenCodeAdapter
from aix.agents.protocol import AgentAdapter, AgentOutcome, AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id
from binaries import make_replay_binary

pytestmark = pytest.mark.anyio

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "opencode"
HELP_OK = "opencode run [message..]  --format --model --session --dir --agent"


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.bin, self.log = tmp, tmp / "bin", tmp / "log"
        self.ws = tmp / "ws"
        self.ws.mkdir()

    def install(self, recording: str | None = None, **kw: object) -> OpenCodeAdapter:
        make_replay_binary(
            self.bin,
            "opencode",
            recording=REC / recording if recording else None,
            log_dir=self.log,
            help_text=HELP_OK,
            **kw,  # type: ignore[arg-type]
        )
        return OpenCodeAdapter(
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
    assert kinds[0] == "started" and kinds[-1] == "finished"
    assert out.status == "completed" and out.failure is None
    assert out.claim == "Added GET /hello. All tests pass."
    assert out.usage.cost_usd == pytest.approx(0.007) and out.usage.input_tokens
    assert out.session_ref == "ses_5e1a2b3c4d5eFgHiJkLmNoPqRs"


async def test_live_recording_parses_as_success(tmp_path: Path) -> None:
    """A stream captured from the real CLI by ``aix dev record`` (M10.1) parses cleanly."""
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("live_read_only.jsonl"), rig.req())
    assert kinds[0] == "started" and kinds[-1] in {"finished", "usage"}
    assert out.status == "completed" and out.failure is None
    assert out.claim and "OK" in out.claim
    assert out.session_ref


async def test_invocation_argv_stdin_and_env(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await run(rig.install("success.jsonl"), rig.req(model="anthropic/x"))
    argv = rig.argv()
    assert argv[:5] == ["run", "--format", "json", "--dir", str(rig.ws)]
    assert argv[argv.index("--model") + 1] == "anthropic/x" and "--agent" not in argv
    assert "add hello" not in " ".join(argv)  # the task is on stdin, never in argv
    assert (rig.log / "stdin.txt").read_text() == "TASK TYPE: implement\nadd hello"
    env = set(json.loads((rig.log / "env.json").read_text()))
    assert "ANTHROPIC_API_KEY" in env and "UNRELATED_SECRET" not in env


async def test_read_only_selects_the_plan_agent(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(
        rig.install("read_only.jsonl"), rig.req(permissions=AgentPermissions(read_only=True))
    )
    assert out.status == "completed" and out.usage.cost_usd is None
    assert rig.argv()[rig.argv().index("--agent") + 1] == "plan"


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


async def test_error_event_with_exit_zero_is_still_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("rate_limit.jsonl", exit_code=0), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.RATE_LIMITED


async def test_permission_rejection_is_surfaced_but_not_fatal(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("permission_rejected.jsonl"), rig.req())
    assert out.status == "completed" and out.claim == "I could not read that file."
    assert "auto-rejecting" in out.stderr_tail


async def test_malformed_stream_lines_do_not_break_the_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("malformed.jsonl"), rig.req())
    assert kinds.count("error") == 3 and out.status == "completed"
    assert out.claim == "ok after garbage"


async def test_stream_without_a_final_step_is_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("no_result.jsonl"), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AGENT_FAILURE
    assert "step_finish" in out.stderr_tail


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
    h = await a.resume("ses_42", rig.req())
    await a.wait(h)
    argv = rig.argv()
    assert argv[argv.index("--session") + 1] == "ses_42"


async def test_probe_ready_degraded_and_unavailable(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = await rig.install(None, version="1.18.26").probe()
    assert spec.health == "ready" and spec.version == "1.18.26"
    assert spec.supports.sessions and spec.cost_class == "medium"

    make_replay_binary(rig.bin, "opencode", version="0.1.0", help_text="run --format")
    degraded = await OpenCodeAdapter(
        env_source={"PATH": f"{rig.bin}{os.pathsep}{os.environ['PATH']}"}
    ).probe()
    assert degraded.health == "degraded"
    assert "--session" in (degraded.health_reason or "")

    gone = await OpenCodeAdapter(env_source={"PATH": str(tmp_path / "empty")}).probe()
    assert gone.health == "unavailable"


def test_satisfies_protocol() -> None:
    assert isinstance(OpenCodeAdapter(), AgentAdapter)
