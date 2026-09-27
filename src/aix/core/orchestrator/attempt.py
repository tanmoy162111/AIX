"""Shared per-attempt machinery for the single-task and multi-task orchestrators (§14.2).

Nothing here decides whether an attempt is accepted; it only runs the agent, streams its events
into the store and persists the raw artifacts.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import anyio

from aix.agents.protocol import AgentHandle, AgentOutcome, AgentRequest
from aix.config.schema import AixConfig
from aix.core.workspace.manager import DiffCapture
from aix.domain.errors import MergeConflict, classify
from aix.domain.execution import ToolCallRecord
from aix.security.policy import Policy
from aix.security.redact import redact_secrets
from aix.store import events as ev

Emit = Callable[..., Awaitable[None]]
"""``emit(event_type, payload, *, task_id=None, attempt_id=None)`` bound to one run."""

OUTPUT_MIN_INTERVAL_S = 1.0
"""At most one ``agent.output`` event per attempt per second (§8.2)."""


async def persist_prompt(stream_path: Path, prompt: str) -> None:
    """Capture the exact (secret-redacted) prompt an attempt was given next to its stream (§15)."""
    target = stream_path.with_name(stream_path.name.replace(".stream.jsonl", ".prompt.txt"))
    await anyio.Path(target.parent).mkdir(parents=True, exist_ok=True)
    await anyio.Path(target).write_text(redact_secrets(prompt))


def model_override(config: AixConfig, agent_id: str) -> str | None:
    override = config.agents.overrides.get(agent_id)
    return override.model if override else None


def timeout_override(config: AixConfig, agent_id: str) -> int | None:
    override = config.agents.overrides.get(agent_id)
    return override.timeout_s if override else None


async def execute_agent(
    adapter: object,
    agent_req: AgentRequest,
    emit: Emit,
    task_id: str,
    attempt_id: str,
    tool_calls: list[ToolCallRecord],
    normalized: list[str],
    on_handle: Callable[[AgentHandle], None] | None = None,
    policy: Policy | None = None,
    violations: list[ev.PolicyViolationPayload] | None = None,
) -> AgentOutcome:
    """Drive one adapter through start -> events -> wait, emitting milestone events.

    ``on_handle`` receives the handle right after ``start`` so a scheduler can cancel the attempt.
    """
    from aix.agents.protocol import AgentAdapter

    assert isinstance(adapter, AgentAdapter)
    handle = None
    last_output = -OUTPUT_MIN_INTERVAL_S
    try:
        handle = await adapter.start(agent_req)
        if on_handle is not None:
            on_handle(handle)
        await emit(
            "attempt.started", ev.AttemptStartedPayload(pid=handle.pid),
            task_id=task_id, attempt_id=attempt_id,
        )  # fmt: skip
        async for event in adapter.events(handle):
            normalized.append(redact_secrets(event.model_dump_json(exclude={"raw"})))
            if event.kind == "tool_call":
                name = str(event.data.get("name", ""))
                tool_calls.append(ToolCallRecord(name=name, ts=event.ts))
                if policy is not None:
                    raw_input = event.data.get("input", "")
                    found = policy.inspect_tool_call(
                        name,
                        raw_input if isinstance(raw_input, str) else json.dumps(raw_input),
                        workspace=str(agent_req.workspace),
                    )
                    if found is not None:
                        payload = ev.PolicyViolationPayload(
                            kind=found[0], detail=redact_secrets(found[1])
                        )
                        if violations is not None:
                            violations.append(payload)
                        await emit(
                            "policy.violation", payload, task_id=task_id, attempt_id=attempt_id
                        )
                await emit(
                    "agent.tool_called", ev.AgentToolCalledPayload(name=name),
                    task_id=task_id, attempt_id=attempt_id,
                )  # fmt: skip
            elif event.kind == "text" and time.monotonic() - last_output >= OUTPUT_MIN_INTERVAL_S:
                last_output = time.monotonic()
                await emit(
                    "agent.output",
                    ev.AgentOutputPayload(
                        text=redact_secrets(str(event.data.get("text", "")))[:200]
                    ),
                    task_id=task_id,
                    attempt_id=attempt_id,
                )
        return await adapter.wait(handle)
    except Exception as exc:
        if handle is not None:
            with anyio.CancelScope(shield=True):
                try:
                    await adapter.cancel(handle, 1)
                    await adapter.wait(handle)
                except Exception:  # best effort: we already have a failure to report
                    pass
        return AgentOutcome(
            exit_code=None, status="failed", failure=classify(exc), stderr_tail=str(exc)
        )


async def persist_artifacts(stream_path: Path, normalized: list[str], capture: DiffCapture) -> None:
    """Make sure the stream file exists (adapters without a byte stream get the normalized one)
    and save the patch next to it."""
    sp = anyio.Path(stream_path)
    await sp.parent.mkdir(parents=True, exist_ok=True)
    if not await sp.exists():
        await sp.write_text("".join(line + "\n" for line in normalized))
    else:  # adapters mirror raw stdout as it arrives; scrub it now that the attempt is over
        raw = await sp.read_text(encoding="utf-8", errors="replace")
        clean = redact_secrets(raw)
        if clean != raw:
            await sp.write_text(clean)
    if capture.patch:
        await anyio.Path(
            stream_path.with_name(stream_path.name.replace(".stream.jsonl", ".patch"))
        ).write_text(redact_secrets(capture.patch))


def conflict_files(exc: MergeConflict) -> list[str]:
    files = exc.details.get("files")
    return [str(f) for f in files] if isinstance(files, list) else []
