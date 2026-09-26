"""Deterministic scripted fake agent used by all non-live tests (PLAYBOOK §27.1)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anyio

from aix.agents.adapters.fake.script import FakeScript, FakeStep, load_scripts
from aix.agents.protocol import AgentAdapter, AgentEvent, AgentHandle, AgentOutcome, AgentRequest
from aix.agents.subprocess import spawn
from aix.domain.agents import AgentSpec, AgentSupports
from aix.domain.enums import Capability, FailureClass
from aix.domain.errors import ToolFailure, UnsupportedError
from aix.domain.execution import Usage

_TASK_TYPE = re.compile(r"^TASK TYPE:\s*(\w+)", re.MULTILINE)
_DEFAULT_CAPS = {c: 0.6 for c in Capability}


def _classify(stderr: str) -> FailureClass:
    low = stderr.lower()
    if "rate limit" in low or "429" in low:
        return FailureClass.RATE_LIMITED
    if "unauthorized" in low or "401" in low or "api key" in low:
        return FailureClass.AUTH_FAILURE
    return FailureClass.AGENT_FAILURE


@dataclass
class _Run:
    req: AgentRequest
    step: FakeStep
    cancel: anyio.Event = field(default_factory=anyio.Event)
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    outcome: AgentOutcome | None = None


class FakeAdapter:
    """In-process agent whose behavior is scripted per test."""

    def __init__(
        self,
        id: str = "fake",
        *,
        scripts: list[FakeScript] | None = None,
        base_dir: Path | None = None,
        capabilities: dict[Capability, float] | None = None,
        health: Literal["ready", "degraded", "unavailable", "disabled"] = "ready",
        health_reason: str | None = None,
        name: str | None = None,
        models: list[str] | None = None,
        default_model: str | None = None,
    ) -> None:
        self._models = list(models or [])
        self._default_model = default_model
        self.id = id
        self._scripts = scripts or []
        self._base_dir = (base_dir or Path.cwd()).resolve()
        self._caps = dict(_DEFAULT_CAPS if capabilities is None else capabilities)
        self._health: Literal["ready", "degraded", "unavailable", "disabled"] = health
        self._reason = health_reason
        self._name = name or id
        self._used = [0] * len(self._scripts)

    async def probe(self) -> AgentSpec:
        return AgentSpec(
            id=self.id,
            name=self._name,
            kind="local",
            capabilities=self._caps,
            supports=AgentSupports(
                streaming=True,
                cancel=True,
                cost_reporting="full",
                model_select=bool(self._models),
            ),
            models=self._models,
            default_model=self._default_model,
            cost_class="free",
            health=self._health,
            health_reason=self._reason,
        )

    def _select(self, req: AgentRequest) -> FakeStep:
        m = _TASK_TYPE.search(req.prompt)
        task_type = m.group(1) if m else None
        for i, script in enumerate(self._scripts):
            want = script.match
            if want.task_type is not None and want.task_type != task_type:
                continue
            if want.prompt_contains is not None and want.prompt_contains not in req.prompt:
                continue
            idx = min(self._used[i], len(script.attempts) - 1)
            self._used[i] += 1
            return script.attempts[idx]
        return FakeStep()

    async def start(self, req: AgentRequest) -> AgentHandle:
        return AgentHandle(
            attempt_id=req.attempt_id, adapter_id=self.id, state=_Run(req, self._select(req))
        )

    async def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        run: _Run = h.state
        yield AgentEvent(kind="started", ts=_now())
        for ev in run.step.events:
            yield AgentEvent(kind=ev.kind, ts=_now(), data=ev.data)
        outcome = await self._execute(run)
        if run.step.usage is not None:
            yield AgentEvent(kind="usage", ts=_now(), data=run.step.usage.model_dump(mode="json"))
        if outcome.status == "failed":
            yield AgentEvent(kind="error", ts=_now(), data={"stderr": outcome.stderr_tail})
        yield AgentEvent(kind="finished", ts=_now(), data={"status": outcome.status})

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        return await self._execute(h.state)

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None:
        h.state.cancel.set()

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        raise UnsupportedError("the fake agent does not support sessions")

    # ---- execution -----------------------------------------------------------------

    async def _execute(self, run: _Run) -> AgentOutcome:
        async with run.lock:
            if run.outcome is None:
                run.outcome = await self._do(run)
            return run.outcome

    async def _do(self, run: _Run) -> AgentOutcome:
        step, req = run.step, run.req
        if step.sleep_s > 0:
            with anyio.move_on_after(min(step.sleep_s, req.timeout_s)):
                await run.cancel.wait()
            if run.cancel.is_set():
                return AgentOutcome(exit_code=None, status="cancelled")
            if step.sleep_s > req.timeout_s:
                return AgentOutcome(exit_code=None, status="timeout", failure=FailureClass.TIMEOUT)
        try:
            await self._apply_effects(step, req.workspace)
        except (ToolFailure, ValueError) as exc:
            return AgentOutcome(
                exit_code=1,
                status="failed",
                failure=FailureClass.TOOL_FAILURE,
                stderr_tail=str(exc),
            )
        usage = step.usage or Usage()
        if step.exit_code != 0:
            return AgentOutcome(
                exit_code=step.exit_code,
                status="failed",
                failure=step.failure or _classify(step.stderr),
                claim=step.claim,
                usage=usage,
                stderr_tail=step.stderr,
            )
        return AgentOutcome(
            exit_code=0,
            status="completed",
            failure=step.failure,
            claim=self._claim(step),
            usage=usage,
            stderr_tail=step.stderr,
        )

    @staticmethod
    def _claim(step: FakeStep) -> str | None:
        if step.planner_output is not None:
            po = step.planner_output
            return po if isinstance(po, str) else json.dumps(po)
        claim = step.claim
        if step.emit_findings is not None:
            block = json.dumps({"findings": step.emit_findings}, indent=2)
            claim = f"{claim or ''}\n```json\n{block}\n```"
        return claim

    async def _apply_effects(self, step: FakeStep, workspace: Path) -> None:
        if step.apply_patch:
            patch = str(self._base_dir / step.apply_patch)
            async with spawn(
                ["git", "apply", "--whitespace=nowarn", patch],
                cwd=workspace,
                env={"PATH": os.environ.get("PATH", "")},
                timeout_s=30,
            ) as proc:
                async for _ in proc.lines():
                    pass
                res = await proc.wait()
            if res.exit_code != 0:
                raise ToolFailure(f"git apply failed: {res.stderr_tail.strip()}")
        await anyio.to_thread.run_sync(_write_files, step, workspace)


def _write_files(step: FakeStep, workspace: Path) -> None:
    """Write scripted files, refusing any path that escapes the workspace."""
    root = workspace.resolve()

    def inside(rel: str) -> Path:
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"path escapes the workspace: {rel}")
        return target

    for rel, content in step.write_files.items():
        target = inside(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    for rel in step.write_outside_scope:
        target = inside(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("written outside the task scope\n", encoding="utf-8")


def _now() -> datetime:
    return datetime.now(UTC)


def create() -> AgentAdapter:
    """Factory used by the registry for the built-in ``fake`` agent.

    ``AIX_FAKE_SCRIPTS`` (a YAML file or a directory of them) scripts its behavior; patch paths in
    the scripts are relative to that file's directory. Used by tests and golden scenarios.
    """
    target = os.environ.get("AIX_FAKE_SCRIPTS")
    if not target:
        return FakeAdapter("fake")
    path = Path(target)
    return FakeAdapter(
        "fake", scripts=load_scripts(path), base_dir=path if path.is_dir() else path.parent
    )
