"""Codex adapter end to end against a fake `codex` binary that replays recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

from aix.agents.adapters.codex import CodexAdapter
from aix.agents.protocol import AgentAdapter, AgentOutcome, AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id
from binaries import make_replay_binary

pytestmark = pytest.mark.anyio

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "codex"
HELP_OK = "Usage: codex exec --json --sandbox --model --cd --config  Commands: resume review"


class Rig:
    def __init__(self, tmp: Path) -> None:
        self.tmp, self.bin, self.log = tmp, tmp / "bin", tmp / "log"
        self.ws = tmp / "ws"
        self.ws.mkdir()

    def install(self, recording: str | None = None, **kw: object) -> CodexAdapter:
        make_replay_binary(
            self.bin,
            "codex",
            recording=REC / recording if recording else None,
            log_dir=self.log,
            help_text=HELP_OK,
            **kw,  # type: ignore[arg-type]
        )
        return CodexAdapter(
            env_source={
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "HOME": str(self.tmp),
                "OPENAI_API_KEY": "sk-test-not-real",
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
    assert out.usage.input_tokens == 24763 and out.usage.cost_usd is None
    assert out.session_ref == "0199a213-81c0-7800-8aa1-bbab2a035a53"


async def test_live_recording_parses_as_success(tmp_path: Path) -> None:
    """A stream captured from the real CLI by ``aix dev record`` (M10.1) parses cleanly."""
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("live_read_only.jsonl"), rig.req())
    assert kinds[0] == "started" and kinds[-1] in {"finished", "usage"}
    assert out.status == "completed" and out.failure is None
    assert out.claim and "OK" in out.claim
    assert out.session_ref


async def test_invocation_argv_stdin_cwd_and_env(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    await run(rig.install("success.jsonl"), rig.req(model="gpt-x"))
    argv = rig.argv()
    assert argv[:2] == ["exec", "--json"]
    assert argv[argv.index("-C") + 1] == str(rig.ws)
    assert argv[argv.index("-s") + 1] == "workspace-write"
    assert argv[-1] == "-" and "gpt-x" in argv
    assert (rig.log / "stdin.txt").read_text() == "TASK TYPE: implement\nadd hello"
    env = set(json.loads((rig.log / "env.json").read_text()))
    assert "OPENAI_API_KEY" in env and "UNRELATED_SECRET" not in env


async def test_read_only_uses_the_read_only_sandbox(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(
        rig.install("read_only.jsonl"), rig.req(permissions=AgentPermissions(read_only=True))
    )
    assert out.status == "completed"
    assert rig.argv()[rig.argv().index("-s") + 1] == "read-only"


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


async def test_turn_failed_with_exit_zero_is_still_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("rate_limit.jsonl", exit_code=0), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.RATE_LIMITED


async def test_nonzero_exit_without_events_uses_stderr(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install(None, exit_code=1, stderr="ERROR: You are not logged in. Run `codex login`\n")
    _, out = await run(a, rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AUTH_FAILURE


async def test_malformed_stream_lines_do_not_break_the_run(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    kinds, out = await run(rig.install("malformed.jsonl"), rig.req())
    assert kinds.count("error") == 3 and out.status == "completed"
    assert out.claim == "ok after garbage"


async def test_stream_without_completion_is_a_failure(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    _, out = await run(rig.install("no_completion.jsonl"), rig.req())
    assert out.status == "failed" and out.failure is FailureClass.AGENT_FAILURE
    assert "turn.completed" in out.stderr_tail


async def test_timeout_and_cancellation(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("no_completion.jsonl", sleep_s=30)
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


async def test_resume_uses_the_exec_resume_subcommand(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    a = rig.install("success.jsonl")
    h = await a.resume("thread-42", rig.req())
    await a.wait(h)
    argv = rig.argv()
    i = argv.index("resume")
    assert argv[i : i + 3] == ["resume", "thread-42", "-"]


async def test_probe_ready_degraded_and_unavailable(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    spec = await rig.install(None, version="codex-cli 0.147.0").probe()
    assert spec.health == "ready" and spec.version == "0.147.0"
    assert spec.supports.sessions and spec.cost_class == "medium"

    make_replay_binary(rig.bin, "codex", version="0.1.0", help_text="--json --model")
    degraded = await CodexAdapter(
        env_source={"PATH": f"{rig.bin}{os.pathsep}{os.environ['PATH']}"}
    ).probe()
    assert degraded.health == "degraded"
    assert "--sandbox" in (degraded.health_reason or "")

    gone = await CodexAdapter(env_source={"PATH": str(tmp_path / "empty")}).probe()
    assert gone.health == "unavailable"


def test_satisfies_protocol() -> None:
    assert isinstance(CodexAdapter(), AgentAdapter)
