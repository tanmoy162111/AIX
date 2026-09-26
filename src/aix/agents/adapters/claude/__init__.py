"""Claude Code adapter (PLAYBOOK §11, Appendix C; notes in docs/adapters/claude.md).

``start``, ``events``, ``wait`` and ``cancel`` must be driven from one task (the process lives in a
task group opened by ``start`` and closed by ``wait``); scopes opened between ``start`` and
``wait`` must be closed before ``wait`` returns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Literal

import yaml

from aix.agents.adapters.claude.argv import build_argv
from aix.agents.adapters.claude.errors import classify_failure
from aix.agents.adapters.claude.parser import StreamState, parse_line
from aix.agents.env import build_agent_env
from aix.agents.manifest import AdapterManifest
from aix.agents.probe import probe_binary
from aix.agents.protocol import AgentAdapter, AgentEvent, AgentHandle, AgentOutcome, AgentRequest
from aix.agents.subprocess import ProcessResult, RunningProcess, spawn
from aix.domain.agents import AgentSpec
from aix.domain.enums import FailureClass
from aix.domain.execution import Usage


def load_manifest() -> AdapterManifest:
    text = resources.files(__package__).joinpath("manifest.yaml").read_text(encoding="utf-8")
    return AdapterManifest.model_validate(yaml.safe_load(text))


@dataclass
class _Session:
    stack: AsyncExitStack
    proc: RunningProcess
    lines: AsyncIterator[str]
    state: StreamState = field(default_factory=StreamState)
    drained: bool = False


def build_outcome(state: StreamState, res: ProcessResult) -> AgentOutcome:
    """Combine what the stream said with how the process ended."""

    def outcome(
        status: Literal["completed", "failed", "timeout", "cancelled"],
        failure: FailureClass | None = None,
        *,
        claim: str | None = None,
        note: str = "",
    ) -> AgentOutcome:
        return AgentOutcome(
            exit_code=res.exit_code,
            status=status,
            failure=failure,
            claim=claim,
            usage=state.usage or Usage(),
            session_ref=state.session_id,
            stderr_tail=note + res.stderr_tail,
        )

    if res.timed_out:
        return outcome("timeout", FailureClass.TIMEOUT)
    if res.cancelled:
        return outcome("cancelled")
    if state.seen_result:
        if state.is_error or res.exit_code != 0:
            text = f"{state.result_text or ''}\n{res.stderr_tail}"
            return outcome("failed", classify_failure(text, state.subtype), claim=state.result_text)
        return outcome("completed", claim=state.result_text)
    if res.exit_code == 0:
        return outcome(
            "failed", FailureClass.AGENT_FAILURE, note="stream ended without a result event\n"
        )
    return outcome("failed", classify_failure(res.stderr_tail))


class ClaudeAdapter:
    """Runs ``claude -p --output-format stream-json`` and normalizes its stream."""

    id = "claude"

    def __init__(
        self,
        manifest: AdapterManifest | None = None,
        *,
        env_source: Mapping[str, str] | None = None,
    ) -> None:
        self.manifest = manifest or load_manifest()
        self._env_source = env_source
        assert self.manifest.binary is not None

    def _env(self, extra: Mapping[str, str]) -> dict[str, str]:
        return build_agent_env(self.manifest.env_allowlist, extra, source=self._env_source)

    async def probe(self) -> AgentSpec:
        m = self.manifest
        result = await probe_binary(m, env=self._env({}))
        return AgentSpec(
            id=m.id,
            name=m.name,
            kind=m.kind,
            version=result.version,
            capabilities=m.capabilities,
            supports=m.supports,
            models=m.models,
            default_model=m.default_model,
            cost_class=m.cost_class,
            health=result.health,
            health_reason=result.reason,
        )

    async def start(self, req: AgentRequest) -> AgentHandle:
        assert self.manifest.binary is not None
        argv = build_argv(self.manifest.binary, req)
        stack = AsyncExitStack()
        try:
            proc = await stack.enter_async_context(
                spawn(
                    argv,
                    cwd=req.workspace,
                    env=self._env(req.env),
                    timeout_s=req.timeout_s,
                    capture_path=req.stream_path,
                    stdin_data=req.prompt.encode("utf-8"),
                )
            )
        except BaseException:
            await stack.aclose()
            raise
        session = _Session(stack=stack, proc=proc, lines=proc.lines())
        return AgentHandle(
            attempt_id=req.attempt_id, adapter_id=self.id, pid=proc.pid, state=session
        )

    async def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        session: _Session = h.state
        async for line in session.lines:
            for event in parse_line(line, session.state, _now()):
                yield event
        session.drained = True

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        session: _Session = h.state
        if not session.drained:
            async for line in session.lines:
                parse_line(line, session.state, _now())
            session.drained = True
        res = await session.proc.wait()
        await session.stack.aclose()
        return build_outcome(session.state, res)

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None:
        await h.state.proc.cancel(grace_s)

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        return await self.start(req.model_copy(update={"session_ref": session_ref}))


def _now() -> datetime:
    return datetime.now(UTC)


def create() -> AgentAdapter:
    """Factory used by the registry."""
    return ClaudeAdapter()
