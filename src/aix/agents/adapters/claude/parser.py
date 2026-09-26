"""Claude Code ``--output-format stream-json`` parser (one JSON object per line).

Maps documented event types to normalized ``AgentEvent``s: ``system/init`` -> started,
``assistant`` text/tool_use blocks -> text/tool_call, ``user`` tool_result blocks -> tool_result,
``result`` -> usage + finished. Malformed lines never abort parsing (§10.5).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import JsonValue

from aix.agents.protocol import AgentEvent
from aix.domain.execution import Usage

_INPUT_PREVIEW = 500
_BAD_LINE_PREVIEW = 200


@dataclass
class StreamState:
    """What the parser learned so far; consumed by ``ClaudeAdapter.wait`` to build the outcome."""

    session_id: str | None = None
    model: str | None = None
    result_text: str | None = None
    subtype: str | None = None
    is_error: bool = False
    seen_result: bool = False
    num_turns: int | None = None
    usage: Usage | None = None
    malformed: int = 0


def _nonneg_int(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def _usage(obj: dict[str, Any], model: str | None) -> Usage:
    raw_usage = obj.get("usage")
    u: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}  # pyright: ignore[reportUnknownVariableType]
    cost = obj.get("total_cost_usd")
    return Usage(
        input_tokens=_nonneg_int(u.get("input_tokens"))
        + _nonneg_int(u.get("cache_creation_input_tokens"))
        + _nonneg_int(u.get("cache_read_input_tokens")),
        output_tokens=_nonneg_int(u.get("output_tokens")),
        cost_usd=float(cost) if isinstance(cost, int | float) and cost >= 0 else None,
        estimated=False,
        model=model,
    )


def _bad(line: str, state: StreamState, now: datetime) -> list[AgentEvent]:
    state.malformed += 1
    return [
        AgentEvent(
            kind="error",
            ts=now,
            data={"malformed": True, "line": line[:_BAD_LINE_PREVIEW]},
        )
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
    obj_d: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
    kind = obj_d.get("type")
    sid = obj_d.get("session_id")
    if isinstance(sid, str):
        state.session_id = sid

    if kind == "system" and obj_d.get("subtype") == "init":
        state.model = obj_d.get("model") if isinstance(obj_d.get("model"), str) else state.model
        return [
            AgentEvent(
                kind="started",
                ts=now,
                data={"session_id": state.session_id, "model": state.model},
                raw=obj_d,
            )
        ]

    events: list[AgentEvent] = []
    if kind in ("assistant", "user"):
        message = obj_d.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if kind == "assistant" and btype == "text":
                events.append(
                    AgentEvent(kind="text", ts=now, data={"text": str(block.get("text", ""))})
                )
            elif kind == "assistant" and btype == "tool_use":
                raw_input = json.dumps(block.get("input", {}), ensure_ascii=False)
                data: dict[str, JsonValue] = {
                    "name": str(block.get("name", "")),
                    "id": str(block.get("id", "")),
                    "input": raw_input[:_INPUT_PREVIEW],
                }
                events.append(AgentEvent(kind="tool_call", ts=now, data=data))
            elif kind == "user" and btype == "tool_result":
                events.append(
                    AgentEvent(
                        kind="tool_result",
                        ts=now,
                        data={
                            "tool_use_id": str(block.get("tool_use_id", "")),
                            "is_error": bool(block.get("is_error", False)),
                        },
                    )
                )
        return events

    if kind == "result":
        state.seen_result = True
        state.subtype = obj_d.get("subtype") if isinstance(obj_d.get("subtype"), str) else None
        state.is_error = bool(obj_d.get("is_error", False))
        result = obj_d.get("result")
        state.result_text = result if isinstance(result, str) else None
        turns = obj_d.get("num_turns")
        state.num_turns = turns if isinstance(turns, int) else None
        state.usage = _usage(obj_d, state.model)
        return [
            AgentEvent(kind="usage", ts=now, data=state.usage.model_dump(mode="json")),
            AgentEvent(
                kind="finished",
                ts=now,
                data={
                    "subtype": state.subtype,
                    "is_error": state.is_error,
                    "num_turns": state.num_turns,
                },
                raw=obj_d,
            ),
        ]
    return []
