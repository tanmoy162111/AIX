"""OpenCode adapter units: argv translation, stream parsing, failure classification."""

from __future__ import annotations

from pathlib import Path

import pytest

import factories as f
from aix.agents.adapters.opencode.argv import build_argv
from aix.agents.adapters.opencode.errors import classify_failure
from aix.agents.adapters.opencode.parser import StreamState, parse_line
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "opencode"
WS = Path("/tmp/ws")
HEAD = ["opencode", "run", "--format", "json", "--dir", str(WS)]


def req(**kw: object) -> AgentRequest:
    perms = kw.pop("perms", AgentPermissions())
    return AgentRequest.model_validate(
        {
            "attempt_id": new_id(IdPrefix.ATTEMPT),
            "workspace": WS,
            "prompt": "SECRET PROMPT BODY",
            "timeout_s": 60,
            "permissions": perms,
            **kw,
        }
    )


def test_argv_default_write_task() -> None:
    assert build_argv("opencode", req()) == HEAD


def test_argv_read_only_model_and_session() -> None:
    argv = build_argv(
        "opencode",
        req(perms=AgentPermissions(read_only=True), model="anthropic/x", session_ref="ses_1"),
    )
    assert argv == [*HEAD, "--agent", "plan", "--model", "anthropic/x", "--session", "ses_1"]


def test_argv_never_carries_the_prompt_or_permission_bypass_flags() -> None:
    argv = build_argv("opencode", req(perms=AgentPermissions(network="allow", write_scope=["x"])))
    assert "SECRET PROMPT BODY" not in " ".join(argv)
    assert not {"--auto", "--yolo", "--dangerously-skip-permissions"} & set(argv)


def parse_file(name: str) -> tuple[StreamState, list[str]]:
    state = StreamState()
    kinds: list[str] = []
    for line in (REC / name).read_text().splitlines():
        kinds += [e.kind for e in parse_line(line, state, f.NOW)]
    return state, kinds


def test_success_stream_is_normalized() -> None:
    state, kinds = parse_file("success.jsonl")
    assert kinds == [
        "started", "text", "tool_call", "tool_result", "usage",
        "tool_call", "tool_result", "usage", "text", "usage", "finished",
    ]  # fmt: skip
    assert state.session_id == "ses_5e1a2b3c4d5eFgHiJkLmNoPqRs"
    assert state.completed and not state.failed and state.step_finishes == 3
    assert state.claim == "Added GET /hello. All tests pass."  # only text after the last tool
    usage = state.usage
    assert usage is not None
    assert usage.input_tokens == 900 + 1200 + 100 + 1200 + 50 + 1300
    assert usage.output_tokens == 40 + 60 + 10 + 20
    assert usage.cost_usd == pytest.approx(0.007)


def test_zero_cost_means_unknown_not_free() -> None:
    state, _ = parse_file("read_only.jsonl")
    assert state.usage is not None and state.usage.cost_usd is None


def test_tool_use_carries_name_input_and_status() -> None:
    state = StreamState()
    events = []
    for line in (REC / "success.jsonl").read_text().splitlines():
        events += parse_line(line, state, f.NOW)
    calls = [e for e in events if e.kind == "tool_call"]
    assert [c.data["name"] for c in calls] == ["read", "edit"]
    assert "app/handler.py" in str(calls[0].data["input"])
    assert all(e.data["ok"] is True for e in events if e.kind == "tool_result")


def test_errored_tool_is_ok_false_and_permission_line_is_not_malformed() -> None:
    state, _ = parse_file("permission_rejected.jsonl")
    assert state.malformed == 0 and len(state.permission_denials) == 1
    assert "external_directory" in state.permission_denials[0]
    assert state.completed
    state2 = StreamState()
    (call, result) = parse_line(
        '{"type":"tool_use","sessionID":"s","part":{"tool":"read","callID":"c",'
        '"state":{"status":"error","error":"nope"}}}',
        state2,
        f.NOW,
    )[-2:]
    assert call.kind == "tool_call" and result.data["ok"] is False


def test_intermediate_tool_call_steps_do_not_finish() -> None:
    state = StreamState()
    events = parse_line(
        '{"type":"step_finish","sessionID":"s","part":{"reason":"tool-calls","tokens":{"input":1}}}',
        state,
        f.NOW,
    )
    assert [e.kind for e in events] == ["started", "usage"] and not state.completed


def test_malformed_lines_become_error_events() -> None:
    state, kinds = parse_file("malformed.jsonl")
    assert kinds.count("error") == 3 and state.malformed == 3
    assert state.completed and state.claim == "ok after garbage"


def test_error_event_fails_and_finishes() -> None:
    state, kinds = parse_file("auth_failure.jsonl")
    assert state.failed and not state.completed
    assert "Authentication failed" in state.error_text
    assert kinds == ["started", "error", "finished"]


def test_error_without_data_message_falls_back_to_the_name() -> None:
    state = StreamState()
    parse_line(
        '{"type":"error","sessionID":"s","error":{"name":"MessageAbortedError"}}', state, f.NOW
    )
    assert "MessageAbortedError" in state.error_text


def test_unknown_and_blank_lines_are_ignored() -> None:
    state = StreamState()
    assert parse_line("", state, f.NOW) == []
    assert parse_line('{"type":"session.idle"}', state, f.NOW) == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ProviderAuthError: invalid x-api-key", FailureClass.AUTH_FAILURE),
        ("429 Too Many Requests: rate limit exceeded", FailureClass.RATE_LIMITED),
        ("ContextOverflowError: prompt is too long", FailureClass.CONTEXT_FAILURE),
        ("Unable to connect (ENOTFOUND api.example.com)", FailureClass.NETWORK_FAILURE),
        ("502 Bad Gateway", FailureClass.NETWORK_FAILURE),
        ("crash", FailureClass.AGENT_FAILURE),
    ],
)
def test_classify_failure(text: str, expected: FailureClass) -> None:
    assert classify_failure(text) is expected
