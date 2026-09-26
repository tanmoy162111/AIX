"""Codex adapter units: argv translation, stream parsing, failure classification."""

from __future__ import annotations

from pathlib import Path

import pytest

import factories as f
from aix.agents.adapters.codex.argv import build_argv
from aix.agents.adapters.codex.errors import classify_failure
from aix.agents.adapters.codex.parser import StreamState, parse_line
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "codex"
WS = Path("/tmp/ws")


def base(sandbox: str) -> list[str]:
    return [
        "codex",
        "exec",
        "--json",
        "-C",
        str(WS),
        "-s",
        sandbox,
        "-c",
        'approval_policy="never"',
    ]


def req(**kw: object) -> AgentRequest:
    perms = kw.pop("perms", AgentPermissions())
    return AgentRequest.model_validate(
        {
            "attempt_id": new_id(IdPrefix.ATTEMPT),
            "workspace": WS,
            "prompt": "PROMPT",
            "timeout_s": 60,
            "permissions": perms,
            **kw,
        }
    )


def test_argv_default_write_task() -> None:
    assert build_argv("codex", req()) == [*base("workspace-write"), "-"]


def test_argv_read_only_and_model() -> None:
    argv = build_argv("codex", req(perms=AgentPermissions(read_only=True), model="gpt-x"))
    assert argv == [*base("read-only"), "-m", "gpt-x", "-"]


def test_argv_network_allow_enables_workspace_network() -> None:
    argv = build_argv("codex", req(perms=AgentPermissions(network="allow")))
    assert argv == [
        *base("workspace-write"),
        "-c",
        "sandbox_workspace_write.network_access=true",
        "-",
    ]


def test_argv_resume_uses_the_subcommand_and_reads_stdin() -> None:
    argv = build_argv("codex", req(session_ref="0199a213-81c0"))
    assert argv == [*base("workspace-write"), "resume", "0199a213-81c0", "-"]


def test_argv_never_contains_the_prompt_or_dangerous_flags() -> None:
    argv = build_argv("codex", req())
    assert "PROMPT" not in argv
    assert not any("dangerously" in a for a in argv)


def parse_file(name: str) -> tuple[StreamState, list[str]]:
    state = StreamState()
    kinds: list[str] = []
    for line in (REC / name).read_text().splitlines():
        kinds += [e.kind for e in parse_line(line, state, f.NOW)]
    return state, kinds


def test_success_stream_is_normalized() -> None:
    state, kinds = parse_file("success.jsonl")
    assert kinds == [
        "started", "tool_call", "tool_result", "tool_call", "tool_result",
        "text", "usage", "finished",
    ]  # fmt: skip
    assert state.thread_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert state.last_message == "Added GET /hello. All tests pass."
    assert state.completed and not state.failed
    assert state.usage is not None
    assert state.usage.input_tokens == 24763 and state.usage.output_tokens == 122
    assert state.usage.cost_usd is None  # codex does not report cost


def test_command_result_reflects_exit_code() -> None:
    state = StreamState()
    events = []
    for line in (REC / "success.jsonl").read_text().splitlines():
        events += parse_line(line, state, f.NOW)
    results = [e for e in events if e.kind == "tool_result"]
    assert results[0].data["ok"] is True and results[0].data["name"] == "command_execution"
    assert results[1].data["name"] == "file_change"
    call = next(e for e in events if e.kind == "tool_call")
    assert call.data["name"] == "command_execution" and "cat app/handler.py" in str(
        call.data["input"]
    )


def test_failed_command_is_a_tool_result_with_ok_false() -> None:
    state = StreamState()
    line = (
        '{"type":"item.completed","item":{"id":"x","type":"command_execution","command":"false",'
        '"exit_code":1,"status":"failed"}}'
    )
    events = parse_line(line, state, f.NOW)
    assert [e.kind for e in events] == ["tool_call", "tool_result"]
    assert events[1].data["ok"] is False


def test_malformed_lines_become_error_events() -> None:
    state, kinds = parse_file("malformed.jsonl")
    assert kinds.count("error") == 3
    assert state.malformed == 3 and state.completed
    assert state.last_message == "ok after garbage"


def test_failure_stream_sets_error_state() -> None:
    state, kinds = parse_file("auth_failure.jsonl")
    assert state.failed and not state.completed
    assert "401" in state.error_text
    assert kinds[-1] == "finished" and "error" in kinds


def test_unknown_and_blank_lines_are_ignored() -> None:
    s = StreamState()
    assert parse_line("", s, f.NOW) == [] and parse_line('{"type":"x.y"}', s, f.NOW) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("exceeded retry limit, last status: 401 Unauthorized", FailureClass.AUTH_FAILURE),
        ("You are not logged in. Run `codex login`", FailureClass.AUTH_FAILURE),
        ("Incorrect API key provided", FailureClass.AUTH_FAILURE),
        ("429 Too Many Requests: rate limit reached", FailureClass.RATE_LIMITED),
        ("You exceeded your current quota", FailureClass.RATE_LIMITED),
        (
            "stream disconnected before completion: error sending request for url",
            FailureClass.NETWORK_FAILURE,
        ),
        ("dns error: failed to lookup address", FailureClass.NETWORK_FAILURE),
        ("context_length_exceeded", FailureClass.CONTEXT_FAILURE),
        ("maximum context length is 128000 tokens", FailureClass.CONTEXT_FAILURE),
        ("panic: something odd", FailureClass.AGENT_FAILURE),
        ("", FailureClass.AGENT_FAILURE),
    ],
)
def test_classify_failure(text: str, expected: FailureClass) -> None:
    assert classify_failure(text) is expected
