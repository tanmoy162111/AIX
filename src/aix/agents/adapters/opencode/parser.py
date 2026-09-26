"""``opencode run --format json`` parser (JSONL events, opencode 1.18.26).

Every line is ``{"type", "timestamp", "sessionID", ...}``: ``step_start`` / ``step_finish`` /
``text`` / ``tool_use`` / ``reasoning`` carry a ``part`` (the CLI emits a part once it is complete;
``tool_use`` only when the tool finished or failed) and ``error`` carries the session ``error``
(``{name, data: {message}}``). There is no final "result" event, so the last ``step_finish`` whose
``reason`` is not ``tool-calls`` marks completion. In JSON mode the CLI can also print a plain
line ``!  permission requested: ... auto-rejecting``; that is reported, not counted as malformed.
The claim is the last assistant text after the last tool call. Malformed lines never abort parsing
(§10.5).
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
    """Parser state consumed by ``OpenCodeAdapter.build_outcome``."""

    session_id: str | None = None
    started: bool = False
    texts: list[str] = field(default_factory=list[str])
    step_finishes: int = 0
    completed: bool = False
    failed: bool = False
    error_messages: list[str] = field(default_factory=list[str])
    permission_denials: list[str] = field(default_factory=list[str])
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    malformed: int = 0

    @property
    def usage(self) -> Usage | None:
        if not self.step_finishes:
            return None
        return Usage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cost_usd=self.cost_usd if self.cost_usd > 0 else None,
        )

    @property
    def claim(self) -> str | None:
        """Assistant text since the last tool call (**not evidence**)."""
        text = "\n".join(self.texts).strip()
        return text or None

    @property
    def error_text(self) -> str:
        return "\n".join(self.error_messages)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _bad(text: str, state: StreamState, now: datetime) -> list[AgentEvent]:
    if "permission requested" in text and "auto-rejecting" in text:
        state.permission_denials.append(text.strip()[:300])
        return [
            AgentEvent(
                kind="error",
                ts=now,
                data={"permission_denied": True, "message": text.strip()[:300]},
            )
        ]
    state.malformed += 1
    return [AgentEvent(kind="error", ts=now, data={"malformed": True, "line": text[:200]})]


def _started(o: dict[str, Any], state: StreamState, now: datetime) -> list[AgentEvent]:
    sid = o.get("sessionID")
    if isinstance(sid, str) and state.session_id is None:
        state.session_id = sid
    if state.started:
        return []
    state.started = True
    return [AgentEvent(kind="started", ts=now, data={"session_id": state.session_id})]


def _tool_events(part: dict[str, Any], now: datetime) -> list[AgentEvent]:
    st = part.get("state")
    state_d: dict[str, Any] = st if isinstance(st, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    name, call_id = str(part.get("tool", "")), str(part.get("callID", part.get("id", "")))
    ok = state_d.get("status") == "completed"
    call: dict[str, JsonValue] = {
        "name": name,
        "id": call_id,
        "input": json.dumps(state_d.get("input", {}), ensure_ascii=False)[:_PREVIEW],
    }
    result: dict[str, JsonValue] = {
        "tool_use_id": call_id,
        "name": name,
        "ok": ok,
        "is_error": not ok,
    }
    return [
        AgentEvent(kind="tool_call", ts=now, data=call),
        AgentEvent(kind="tool_result", ts=now, data=result),
    ]


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
    raw_part = o.get("part")
    part: dict[str, Any] = raw_part if isinstance(raw_part, dict) else {}  # pyright: ignore[reportUnknownVariableType]

    if kind == "error":
        events = _started(o, state, now)
        err = o.get("error")
        e: dict[str, Any] = err if isinstance(err, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        data = e.get("data")
        d: dict[str, Any] = data if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        message = str(d.get("message") or e.get("name") or "")
        name = str(e.get("name", ""))
        state.error_messages.append(f"{name}: {message}".strip(": "))
        state.failed = True
        events.append(AgentEvent(kind="error", ts=now, data={"message": message, "name": name}))
        events.append(AgentEvent(kind="finished", ts=now, data={"status": "failed"}, raw=o))
        return events

    if kind not in ("step_start", "step_finish", "text", "tool_use", "reasoning"):
        return []
    events = _started(o, state, now)

    if kind == "text":
        content = str(part.get("text", ""))
        state.texts.append(content)
        events.append(AgentEvent(kind="text", ts=now, data={"text": content}))
    elif kind == "tool_use":
        state.texts.clear()  # the claim is what the agent says after its last tool call
        events += _tool_events(part, now)
    elif kind == "step_finish":
        state.step_finishes += 1
        tokens = part.get("tokens")
        t: dict[str, Any] = tokens if isinstance(tokens, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        cache = t.get("cache")
        c: dict[str, Any] = cache if isinstance(cache, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        state.input_tokens += _int(t.get("input")) + _int(c.get("read")) + _int(c.get("write"))
        state.output_tokens += _int(t.get("output")) + _int(t.get("reasoning"))
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and cost > 0:
            state.cost_usd += float(cost)
        usage = state.usage
        assert usage is not None
        events.append(AgentEvent(kind="usage", ts=now, data=usage.model_dump(mode="json")))
        if part.get("reason") != "tool-calls":
            state.completed = True
            events.append(AgentEvent(kind="finished", ts=now, data={"status": "completed"}, raw=o))
    return events
