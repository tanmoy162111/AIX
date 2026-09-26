"""``codex exec --json`` parser (JSONL events).

Maps ``thread.started`` -> started, tool-like items (command_execution, file_change,
mcp_tool_call, web_search) -> tool_call/tool_result, ``agent_message`` -> text,
``turn.completed`` -> usage + finished, ``turn.failed`` / ``error`` -> error. Malformed lines never
abort parsing (§10.5). Top-level ``error`` events are non-fatal notices; only ``turn.failed`` or a
non-zero exit fails the attempt.
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
_TOOL_ITEMS = frozenset({"command_execution", "file_change", "mcp_tool_call", "web_search"})


@dataclass
class StreamState:
    """Parser state consumed by ``CodexAdapter.build_outcome``."""

    thread_id: str | None = None
    last_message: str | None = None
    completed: bool = False
    failed: bool = False
    error_messages: list[str] = field(default_factory=list[str])
    usage: Usage | None = None
    malformed: int = 0
    started_ids: set[str] = field(default_factory=set[str])

    @property
    def error_text(self) -> str:
        return "\n".join(self.error_messages)


def _nonneg(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _tool_input(item: dict[str, Any]) -> str:
    kind = item.get("type")
    if kind == "command_execution":
        return str(item.get("command", ""))[:_PREVIEW]
    if kind == "file_change":
        return json.dumps(item.get("changes", []), ensure_ascii=False)[:_PREVIEW]
    if kind == "web_search":
        return str(item.get("query", ""))[:_PREVIEW]
    return json.dumps({"server": item.get("server"), "tool": item.get("tool")}, ensure_ascii=False)[
        :_PREVIEW
    ]


def _tool_ok(item: dict[str, Any]) -> bool:
    if item.get("type") == "command_execution" and isinstance(item.get("exit_code"), int):
        return item["exit_code"] == 0
    return item.get("status") == "completed"


def _bad(text: str, state: StreamState, now: datetime) -> list[AgentEvent]:
    state.malformed += 1
    return [AgentEvent(kind="error", ts=now, data={"malformed": True, "line": text[:200]})]


def _add_usage(state: StreamState, usage: Any) -> Usage:
    u: dict[str, Any] = usage if isinstance(usage, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    prev = state.usage or Usage(input_tokens=0, output_tokens=0)
    state.usage = Usage(
        input_tokens=(prev.input_tokens or 0) + _nonneg(u.get("input_tokens")),
        output_tokens=(prev.output_tokens or 0) + _nonneg(u.get("output_tokens")),
        cost_usd=None,
    )
    return state.usage


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

    if kind == "thread.started":
        tid = o.get("thread_id")
        state.thread_id = tid if isinstance(tid, str) else state.thread_id
        return [AgentEvent(kind="started", ts=now, data={"thread_id": state.thread_id}, raw=o)]

    if kind in ("item.started", "item.completed"):
        item = o.get("item")
        if not isinstance(item, dict):
            return []
        it: dict[str, Any] = item  # pyright: ignore[reportUnknownVariableType]
        itype, iid = it.get("type"), str(it.get("id", ""))
        if itype == "agent_message" and kind == "item.completed":
            msg = str(it.get("text", ""))
            state.last_message = msg
            return [AgentEvent(kind="text", ts=now, data={"text": msg})]
        if itype == "error" and kind == "item.completed":
            return [AgentEvent(kind="error", ts=now, data={"message": str(it.get("message", ""))})]
        if itype in _TOOL_ITEMS:
            events: list[AgentEvent] = []
            if iid not in state.started_ids:
                state.started_ids.add(iid)
                call: dict[str, JsonValue] = {"name": itype, "id": iid, "input": _tool_input(it)}
                events.append(AgentEvent(kind="tool_call", ts=now, data=call))
            if kind == "item.completed":
                ok = _tool_ok(it)
                events.append(
                    AgentEvent(
                        kind="tool_result",
                        ts=now,
                        data={"tool_use_id": iid, "name": itype, "ok": ok, "is_error": not ok},
                    )
                )
            return events
        return []

    if kind == "turn.completed":
        state.completed = True
        usage = _add_usage(state, o.get("usage"))
        return [
            AgentEvent(kind="usage", ts=now, data=usage.model_dump(mode="json")),
            AgentEvent(kind="finished", ts=now, data={"status": "completed"}, raw=o),
        ]

    if kind in ("turn.failed", "error"):
        err = o.get("error")
        message = str(
            (err.get("message") if isinstance(err, dict) else None) or o.get("message") or ""  # pyright: ignore[reportUnknownMemberType]
        )
        state.error_messages.append(message)
        events = [AgentEvent(kind="error", ts=now, data={"message": message})]
        if kind == "turn.failed":
            state.failed = True
            events.append(AgentEvent(kind="finished", ts=now, data={"status": "failed"}, raw=o))
        return events
    return []
