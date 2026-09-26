"""Claude adapter units: argv translation, stream parsing, failure classification."""

from __future__ import annotations

from pathlib import Path

import pytest

import factories as f
from aix.agents.adapters.claude.argv import build_argv
from aix.agents.adapters.claude.errors import classify_failure
from aix.agents.adapters.claude.parser import StreamState, parse_line
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "claude"
BASE = ["claude", "-p", "--output-format", "stream-json", "--verbose"]


def req(**perm: object) -> AgentRequest:
    return AgentRequest(
        attempt_id=new_id(IdPrefix.ATTEMPT),
        workspace=Path("/tmp/ws"),
        prompt="PROMPT",
        timeout_s=60,
        permissions=AgentPermissions.model_validate(perm),
    )


# ---- permission translation (snapshot-style) -----------------------------------------


def test_argv_default_write_task() -> None:
    assert build_argv("claude", req()) == [*BASE, "--permission-mode", "acceptEdits"]


def test_argv_prompt_is_never_in_argv() -> None:
    assert "PROMPT" not in build_argv("claude", req())


def test_argv_model_and_resume() -> None:
    r = req().model_copy(update={"model": "opus", "session_ref": "sess-1"})
    assert build_argv("claude", r) == [
        *BASE, "--model", "opus", "--resume", "sess-1", "--permission-mode", "acceptEdits",
    ]  # fmt: skip


def test_argv_read_only() -> None:
    assert build_argv("claude", req(read_only=True)) == [
        *BASE,
        "--permission-mode", "dontAsk",
        "--allowedTools", "Read,Glob,Grep",
        "--disallowedTools", "Edit,Write,NotebookEdit,Bash",
    ]  # fmt: skip


def test_argv_write_scope_becomes_native_path_rules() -> None:
    argv = build_argv("claude", req(write_scope=["app/**", "tests/**"]))
    assert argv == [
        *BASE,
        "--permission-mode", "dontAsk",
        "--allowedTools",
        "Read,Glob,Grep,Edit(app/**),Write(app/**),Edit(tests/**),Write(tests/**)",
    ]  # fmt: skip


def test_argv_allowed_tools_are_appended_and_network_deny_blocks_web_tools() -> None:
    argv = build_argv("claude", req(allowed_tools=["Bash(pytest *)"], network="deny"))
    assert argv == [
        *BASE,
        "--permission-mode", "acceptEdits",
        "--allowedTools", "Bash(pytest *)",
        "--disallowedTools", "WebFetch,WebSearch",
    ]  # fmt: skip


# ---- stream parsing ----------------------------------------------------------------------


def parse_file(name: str) -> tuple[StreamState, list[str]]:
    state = StreamState()
    kinds: list[str] = []
    for line in (REC / name).read_text().splitlines():
        kinds += [e.kind for e in parse_line(line, state, f.NOW)]
    return state, kinds


def test_success_stream_is_normalized() -> None:
    state, kinds = parse_file("success.jsonl")
    assert kinds == ["started", "text", "tool_call", "tool_result", "text", "usage", "finished"]
    assert state.session_id == "11111111-2222-3333-4444-555555555555"
    assert state.result_text == "Done. The endpoint is added and all tests pass."
    assert state.seen_result and not state.is_error
    assert state.usage is not None
    assert state.usage.cost_usd == 0.0421 and state.usage.estimated is False
    assert state.usage.input_tokens == 470 + 1000 + 4000  # input + cache creation + cache read
    assert state.usage.output_tokens == 92
    assert state.usage.model == "claude-sonnet-5"


def test_tool_call_event_carries_name_and_truncated_input() -> None:
    state = StreamState()
    events = []
    for line in (REC / "success.jsonl").read_text().splitlines():
        events += parse_line(line, state, f.NOW)
    call = next(e for e in events if e.kind == "tool_call")
    assert call.data["name"] == "Edit" and call.data["id"] == "toolu_01"
    assert isinstance(call.data["input"], str) and len(call.data["input"]) <= 500
    res = next(e for e in events if e.kind == "tool_result")
    assert res.data["tool_use_id"] == "toolu_01" and res.data["is_error"] is False


def test_malformed_lines_become_error_events_and_parsing_continues() -> None:
    state = StreamState()
    events = []
    for line in (REC / "malformed.jsonl").read_text().splitlines():
        events += parse_line(line, state, f.NOW)
    errors = [e for e in events if e.kind == "error"]
    assert len(errors) == 3  # two broken lines and one non-object JSON value
    assert all(e.data.get("malformed") is True for e in errors)
    assert state.malformed == 3
    assert state.seen_result and state.result_text == "ok after garbage"


def test_blank_lines_are_ignored() -> None:
    assert parse_line("   ", StreamState(), f.NOW) == []


def test_unknown_event_types_are_ignored() -> None:
    assert parse_line('{"type":"brand_new_thing"}', StreamState(), f.NOW) == []


def test_error_result_sets_flags() -> None:
    state, kinds = parse_file("max_turns.jsonl")
    assert state.is_error and state.subtype == "error_max_turns" and kinds[-1] == "finished"


# ---- failure classification --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "subtype", "expected"),
    [
        ("Invalid API key · Please run /login", None, FailureClass.AUTH_FAILURE),
        ("OAuth token has expired", None, FailureClass.AUTH_FAILURE),
        ("authentication_error: bad credentials", None, FailureClass.AUTH_FAILURE),
        ('API Error: 429 {"type":"rate_limit_error"}', None, FailureClass.RATE_LIMITED),
        ("Claude usage limit reached", None, FailureClass.RATE_LIMITED),
        ("overloaded_error 529", None, FailureClass.RATE_LIMITED),
        ("connect ECONNRESET 1.2.3.4:443", None, FailureClass.NETWORK_FAILURE),
        ("getaddrinfo ENOTFOUND api.anthropic.com", None, FailureClass.NETWORK_FAILURE),
        ("Prompt is too long", None, FailureClass.CONTEXT_FAILURE),
        ("anything", "error_max_budget_usd", FailureClass.BUDGET_EXCEEDED),
        ("anything", "error_max_turns", FailureClass.AGENT_FAILURE),
        ("segmentation fault", None, FailureClass.AGENT_FAILURE),
        ("", None, FailureClass.AGENT_FAILURE),
    ],
)
def test_classify_failure(text: str, subtype: str | None, expected: FailureClass) -> None:
    assert classify_failure(text, subtype) is expected
