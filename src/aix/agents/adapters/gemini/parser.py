"""``gemini --output-format stream-json`` parser (JSONL events, gemini-cli 0.55.1).

Event types (from ``JsonStreamEventType`` in the CLI): ``init`` (session_id, model), ``message``
(role user|assistant, content, delta), ``tool_use`` (tool_name, tool_id, parameters),
``tool_result`` (tool_id, status success|error, output, error), ``error`` (severity, message) and
``result`` (status success|error, error{type,message}, stats{input_tokens, output_tokens, ...}).

The claim is the assistant text after the last tool call. ``error`` events are notices; only a
``result`` with ``status: error``, a non-zero exit or a missing ``result`` fails the attempt.
Malformed lines never abort parsing (§10.5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import JsonValue

from aix.agents.protocol import AgentEvent
from aix.domain.execution import Usage

_PREVIEW = 500


@dataclass
class StreamState:
    """Parser state consumed by ``GeminiAdapter.build_outcome``."""

    session_id: str | None = None
    model: str | None = None
    turn_text: list[str] = field(default_factory=list[str])
    result_seen: bool = False
    result_ok: bool = False
    result_error: str = ""
    error_messages: list[str] = field(default_factory=list[str])
    usage: Usage | None = None
    malformed: int = 0
    tool_names: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def claim(self) -> str | None:
        """Assistant text since the last tool call (**not evidence**)."""
        text = "".join(self.turn_text).strip()
        return text or None

    @property
    def error_text(self) -> str:
        return "\n".join([self.result_error, *self.error_messages]).strip()


def _nonneg(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _bad(text: str, state: StreamState, now: datetime) -> list[AgentEvent]:
    state.malformed += 1
    return [AgentEvent(kind="error", ts=now, data={"malformed": True, "line": text[:200]})]


def parse_line(line: str, state: StreamState, now: datetime) -> list[AgentEvent]:
    """Parse one stream line, updating ``state``; returns zero or more normalized events."""
    text = line.strip()
    if not text:
        return []
    try:
        obj = json.loads(text)
    except ValueError:
        return _bad(text, state, now)
    if not isinstance(obj, dict):
        return _bad(text, state, now)
    o: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
    kind = o.get("type")

    if kind == "init":
        sid, model = o.get("session_id"), o.get("model")
        state.session_id = sid if isinstance(sid, str) else state.session_id
        state.model = model if isinstance(model, str) else state.model
        data: dict[str, JsonValue] = {"session_id": state.session_id, "model": state.model}
        return [AgentEvent(kind="started", ts=now, data=data, raw=o)]

    if kind == "message":
        if o.get("role") != "assistant":
            return []
        content = str(o.get("content", ""))
        state.turn_text.append(content)
        return [AgentEvent(kind="text", ts=now, data={"text": content})]

    if kind == "tool_use":
        tool_id, name = str(o.get("tool_id", "")), str(o.get("tool_name", ""))
        state.tool_names[tool_id] = name
        state.turn_text.clear()  # the claim is what the agent says after its last tool call
        params = json.dumps(o.get("parameters", {}), ensure_ascii=False)[:_PREVIEW]
        return [
            AgentEvent(
                kind="tool_call", ts=now, data={"name": name, "id": tool_id, "input": params}
            )
        ]

    if kind == "tool_result":
        tool_id = str(o.get("tool_id", ""))
        ok = o.get("status") == "success"
        return [
            AgentEvent(
                kind="tool_result",
                ts=now,
                data={
                    "tool_use_id": tool_id,
                    "name": state.tool_names.get(tool_id, ""),
                    "ok": ok,
                    "is_error": not ok,
                },
            )
        ]

    if kind == "error":
        message = str(o.get("message", ""))
        state.error_messages.append(message)
        severity = str(o.get("severity", "error"))
        return [AgentEvent(kind="error", ts=now, data={"message": message, "severity": severity})]

    if kind == "result":
        state.result_seen = True
        state.result_ok = o.get("status") == "success"
        err = o.get("error")
        if isinstance(err, dict):
            e: dict[str, Any] = err  # pyright: ignore[reportUnknownVariableType]
            state.result_error = f"{e.get('type', '')}: {e.get('message', '')}".strip(": ")
        stats = o.get("stats")
        s: dict[str, Any] = stats if isinstance(stats, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        state.usage = Usage(
            input_tokens=_nonneg(s.get("input_tokens")),
            output_tokens=_nonneg(s.get("output_tokens")),
            cost_usd=None,
            model=state.model,
        )
        status = "completed" if state.result_ok else "failed"
        events = [AgentEvent(kind="usage", ts=now, data=state.usage.model_dump(mode="json"))]
        if not state.result_ok:
            events.append(AgentEvent(kind="error", ts=now, data={"message": state.result_error}))
        events.append(AgentEvent(kind="finished", ts=now, data={"status": status}, raw=o))
        return events
    return []
