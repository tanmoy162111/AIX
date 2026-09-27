"""Gemini adapter units: argv translation, stream parsing, failure classification."""

from __future__ import annotations

from pathlib import Path

import pytest

import factories as f
from aix.agents.adapters.gemini.argv import INSTRUCTION, build_argv
from aix.agents.adapters.gemini.errors import classify_failure
from aix.agents.adapters.gemini.parser import StreamState, parse_line
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id

REC = Path(__file__).resolve().parents[1] / "fixtures" / "recordings" / "gemini"
WS = Path("/tmp/ws")
HEAD = ["gemini", "--output-format", "stream-json", "--approval-mode"]


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
    assert build_argv("gemini", req()) == [
        *HEAD, "auto_edit", "--skip-trust", "--prompt", INSTRUCTION,
    ]  # fmt: skip


def test_argv_read_only_uses_plan_mode_and_model_and_resume() -> None:
    argv = build_argv(
        "gemini", req(perms=AgentPermissions(read_only=True), model="gemini-x", session_ref="s-9")
    )
    assert argv == [
        *HEAD, "plan", "--skip-trust", "--model", "gemini-x", "--resume", "s-9",
        "--prompt", INSTRUCTION,
    ]  # fmt: skip


def test_argv_never_carries_the_prompt_or_yolo() -> None:
    argv = build_argv(
        "gemini", req(perms=AgentPermissions(network="allow", write_scope=["src/**"]))
    )
    assert "SECRET PROMPT BODY" not in " ".join(argv)
    assert not any(a in ("--yolo", "-y", "yolo") for a in argv)


def parse_file(name: str) -> tuple[StreamState, list[str]]:
    state = StreamState()
    kinds: list[str] = []
    for line in (REC / name).read_text().splitlines():
        kinds += [e.kind for e in parse_line(line, state, f.NOW)]
    return state, kinds


def test_success_stream_is_normalized() -> None:
    state, kinds = parse_file("success.jsonl")
    assert kinds == [
        "started", "text", "tool_call", "tool_result", "tool_call", "tool_result",
        "text", "text", "usage", "finished",
    ]  # fmt: skip
    assert state.session_id == "b1c2d3e4-1111-4222-8333-444455556666"
    assert state.model == "gemini-2.5-pro"
    assert state.result_seen and state.result_ok
    assert state.claim == "Added GET /hello. All tests pass."  # only the text after the last tool
    assert state.usage is not None
    assert state.usage.input_tokens == 15000 and state.usage.output_tokens == 200
    assert state.usage.cost_usd is None


def test_tool_results_carry_the_tool_name_and_status() -> None:
    state = StreamState()
    events = []
    for line in (REC / "success.jsonl").read_text().splitlines():
        events += parse_line(line, state, f.NOW)
    results = [e for e in events if e.kind == "tool_result"]
    assert [r.data["name"] for r in results] == ["read_file", "replace"]
    assert all(r.data["ok"] is True for r in results)
    call = next(e for e in events if e.kind == "tool_call")
    assert call.data["name"] == "read_file" and "app/handler.py" in str(call.data["input"])


def test_failed_tool_is_ok_false() -> None:
    state = StreamState()
    parse_line(
        '{"type":"tool_use","tool_name":"shell","tool_id":"t1","parameters":{}}', state, f.NOW
    )
    (event,) = parse_line(
        '{"type":"tool_result","tool_id":"t1","status":"error","error":{"message":"denied"}}',
        state,
        f.NOW,
    )
    assert event.data["ok"] is False and event.data["is_error"] is True


def test_user_messages_are_not_text_events() -> None:
    state = StreamState()
    assert parse_line('{"type":"message","role":"user","content":"hi"}', state, f.NOW) == []


def test_malformed_lines_become_error_events() -> None:
    state, kinds = parse_file("malformed.jsonl")
    assert kinds.count("error") == 3 and state.malformed == 3
    assert state.result_seen and state.result_ok and state.claim == "ok after garbage"


def test_error_result_sets_failure_state() -> None:
    state, kinds = parse_file("auth_failure.jsonl")
    assert state.result_seen and not state.result_ok
    assert "GEMINI_API_KEY" in state.error_text
    assert kinds[-1] == "finished" and "error" in kinds


def test_error_events_are_non_fatal_notices() -> None:
    state, _ = parse_file("rate_limit.jsonl")
    assert state.error_messages == ["Retrying after 429 RESOURCE_EXHAUSTED"]


def test_unknown_and_blank_lines_are_ignored() -> None:
    state = StreamState()
    assert parse_line("", state, f.NOW) == []
    assert parse_line('{"type":"something_new"}', state, f.NOW) == []


@pytest.mark.parametrize(
    ("text", "code", "expected"),
    [
        ("anything", 41, FailureClass.AUTH_FAILURE),
        (
            "Please set an Auth method in your settings.json or specify GEMINI_API_KEY",
            1,
            FailureClass.AUTH_FAILURE,
        ),
        ("API key not valid. Please pass a valid API key.", 1, FailureClass.AUTH_FAILURE),
        ("[429] RESOURCE_EXHAUSTED: quota exceeded", 1, FailureClass.RATE_LIMITED),
        ("The input token count (1200000) exceeds the maximum", 1, FailureClass.CONTEXT_FAILURE),
        ("exception TypeError: fetch failed", 1, FailureClass.NETWORK_FAILURE),
        ("503 The model is overloaded", 1, FailureClass.NETWORK_FAILURE),
        ("something odd", 1, FailureClass.AGENT_FAILURE),
        ("something odd", None, FailureClass.AGENT_FAILURE),
    ],
)
def test_classify_failure(text: str, code: int | None, expected: FailureClass) -> None:
    assert classify_failure(text, code) is expected


def test_ineligible_account_is_an_auth_failure() -> None:
    """Observed live (M10.1): the retired free tier answers IneligibleTierError, exit 1."""
    text = (
        "Error authenticating: IneligibleTierError: This client is no longer supported for "
        "Gemini Code Assist for individuals. reasonCode: 'UNSUPPORTED_CLIENT'"
    )
    assert classify_failure(text, 1) is FailureClass.AUTH_FAILURE
