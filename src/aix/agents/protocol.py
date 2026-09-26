"""AgentAdapter protocol and the normalized request/event/outcome types (PLAYBOOK §10.1).

An adapter owns every vendor specific. The control plane only ever sees these types. The agent's
final message is a *claim* (``AgentOutcome.claim``), never verification evidence (§10.4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from aix.domain.agents import AgentSpec
from aix.domain.base import DomainModel, UtcDatetime
from aix.domain.enums import FailureClass
from aix.domain.execution import Usage
from aix.domain.ids import AttemptId


class AgentPermissions(DomainModel):
    """Policy translated into what an agent may do; each adapter maps it to native flags (§11)."""

    read_only: bool = False
    allowed_tools: list[str] | None = None
    """``None`` means the agent's default tool set."""
    write_scope: list[str] = Field(default_factory=list)
    """Globs the agent may write; empty with ``read_only=False`` means the whole workspace."""
    network: Literal["deny", "provider_default", "allow"] = "provider_default"


class ContainerSpec(DomainModel):
    """Run the agent process inside a container (§20.3): only the workspace is mounted."""

    runtime: Literal["docker", "podman"]
    image: str
    network: Literal["none", "bridge"] = "none"
    memory: str | None = None
    pids_limit: int = 512
    uid: int
    gid: int


class AgentRequest(DomainModel):
    """Everything an adapter needs to run one attempt."""

    attempt_id: AttemptId
    workspace: Path
    prompt: str
    """Rendered from a template (Appendix B); includes the context pack."""
    model: str | None = None
    timeout_s: int = Field(ge=1)
    permissions: AgentPermissions = Field(default_factory=AgentPermissions)
    env: dict[str, str] = Field(default_factory=dict[str, str])
    """Already filtered by the secrets policy (§20.5)."""
    session_ref: str | None = None
    container: ContainerSpec | None = None
    """When set, the CLI runs in this container instead of on the host (adapters that spawn a
    process honour it; in-process agents ignore it)."""
    stream_path: Path | None = None
    """Where to mirror the raw agent stdout (``.aix/runs/<run>/<attempt>.stream.jsonl``)."""


EventKind = Literal["started", "text", "tool_call", "tool_result", "usage", "error", "finished"]


class AgentEvent(DomainModel):
    """A vendor-neutral stream event."""

    kind: EventKind
    ts: UtcDatetime
    data: dict[str, JsonValue] = Field(default_factory=dict[str, JsonValue])
    raw: dict[str, JsonValue] | None = None


class AgentOutcome(DomainModel):
    """Result of a finished agent process."""

    exit_code: int | None
    status: Literal["completed", "failed", "timeout", "cancelled"]
    failure: FailureClass | None = None
    claim: str | None = None
    """The agent's final message. NOT evidence."""
    usage: Usage = Field(default_factory=Usage)
    session_ref: str | None = None
    stderr_tail: str = ""


class AgentHandle(BaseModel):
    """Opaque handle to a started agent. ``state`` is private to the adapter that made it."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    attempt_id: AttemptId
    adapter_id: str
    pid: int | None = None
    state: Any = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Translation layer between the control plane and one agent."""

    id: str

    async def probe(self) -> AgentSpec:
        """Report presence, version, auth and capabilities (absence is health, not an error)."""
        ...

    async def start(self, req: AgentRequest) -> AgentHandle:
        """Spawn the agent and return immediately."""
        ...

    def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        """Normalized event stream for a started agent."""
        ...

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        """Wait for exit; classify the failure if any."""
        ...

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None:
        """Terminate the agent (TERM, then KILL after ``grace_s``)."""
        ...

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        """Continue a session; raise ``UnsupportedError`` if the agent cannot."""
        ...
