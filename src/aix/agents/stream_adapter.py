"""Base class for adapters that drive a CLI producing a JSONL event stream.

Subclasses supply four hooks (argv, initial parser state, line parser, outcome builder); this
class owns probing, process lifetime, streaming, cancellation and resume.

``start``, ``events``, ``wait`` and ``cancel`` must be driven from one task: the process lives in a
task group opened by ``start`` and closed by ``wait``, so any scope opened between the two must be
closed before ``wait`` returns.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime

from aix.agents.container import container_name, kill_container, wrap_command
from aix.agents.env import build_agent_env
from aix.agents.manifest import AdapterManifest
from aix.agents.probe import probe_binary
from aix.agents.protocol import AgentEvent, AgentHandle, AgentOutcome, AgentRequest
from aix.agents.subprocess import ProcessResult, RunningProcess, spawn
from aix.domain.agents import AgentSpec


@dataclass
class Session[S]:
    """Per-attempt state kept in ``AgentHandle.state``."""

    stack: AsyncExitStack
    proc: RunningProcess
    lines: AsyncIterator[str]
    state: S
    drained: bool = False
    container: tuple[str, str] | None = None
    """``(runtime, name)`` when the attempt runs in a container."""


def _now() -> datetime:
    return datetime.now(UTC)


class StreamingCliAdapter[S]:
    """Shared machinery for JSONL-streaming CLI agents."""

    id: str

    def __init__(
        self, manifest: AdapterManifest, *, env_source: Mapping[str, str] | None = None
    ) -> None:
        if manifest.binary is None:
            raise ValueError(f"manifest {manifest.id!r} has no binary")
        self.manifest = manifest
        self.id = manifest.id
        self._env_source = env_source

    # ---- hooks -----------------------------------------------------------------------

    def build_argv(self, req: AgentRequest) -> list[str]:
        """Command line for ``req`` (the prompt goes on stdin, not argv)."""
        raise NotImplementedError

    def new_state(self) -> S:
        """Fresh parser state for one attempt."""
        raise NotImplementedError

    def parse_line(self, line: str, state: S, now: datetime) -> list[AgentEvent]:
        """Normalize one stdout line and update ``state``."""
        raise NotImplementedError

    def build_outcome(self, state: S, res: ProcessResult) -> AgentOutcome:
        """Combine the parsed stream with the process result."""
        raise NotImplementedError

    # ---- protocol ---------------------------------------------------------------------

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
        stack = AsyncExitStack()
        argv, env = self.build_argv(req), self._env(req.env)
        container: tuple[str, str] | None = None
        if req.container is not None:
            argv, env = wrap_command(argv, req, env)
            container = (req.container.runtime, container_name(req))
        try:
            proc = await stack.enter_async_context(
                spawn(
                    argv,
                    cwd=req.workspace,
                    env=env,
                    timeout_s=req.timeout_s,
                    capture_path=req.stream_path,
                    stdin_data=req.prompt.encode("utf-8"),
                )
            )
        except BaseException:
            await stack.aclose()
            raise
        session: Session[S] = Session(
            stack=stack, proc=proc, lines=proc.lines(), state=self.new_state(),
            container=container,
        )  # fmt: skip
        return AgentHandle(
            attempt_id=req.attempt_id, adapter_id=self.id, pid=proc.pid, state=session
        )

    async def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        session: Session[S] = h.state
        async for line in session.lines:
            for event in self.parse_line(line, session.state, _now()):
                yield event
        session.drained = True

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        session: Session[S] = h.state
        if not session.drained:
            async for line in session.lines:
                self.parse_line(line, session.state, _now())
            session.drained = True
        res = await session.proc.wait()
        await session.stack.aclose()
        return self.build_outcome(session.state, res)

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None:
        await h.state.proc.cancel(grace_s)
        if h.state.container is not None:  # the runtime client dying does not stop the container
            runtime, name = h.state.container
            await kill_container(runtime, name)

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        return await self.start(req.model_copy(update={"session_ref": session_ref}))


__all__ = ["Session", "StreamingCliAdapter"]
