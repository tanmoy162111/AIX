"""Codex CLI adapter (PLAYBOOK §11, Appendix C; notes in docs/adapters/codex.md)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from importlib import resources
from typing import Literal

import yaml

from aix.agents.adapters.codex.argv import build_argv
from aix.agents.adapters.codex.errors import classify_failure
from aix.agents.adapters.codex.parser import StreamState, parse_line
from aix.agents.manifest import AdapterManifest
from aix.agents.protocol import AgentAdapter, AgentEvent, AgentOutcome, AgentRequest
from aix.agents.stream_adapter import StreamingCliAdapter
from aix.agents.subprocess import ProcessResult
from aix.domain.enums import FailureClass
from aix.domain.execution import Usage


def load_manifest() -> AdapterManifest:
    text = resources.files(__package__).joinpath("manifest.yaml").read_text(encoding="utf-8")
    return AdapterManifest.model_validate(yaml.safe_load(text))


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
            session_ref=state.thread_id,
            stderr_tail=note + res.stderr_tail,
        )

    if res.timed_out:
        return outcome("timeout", FailureClass.TIMEOUT)
    if res.cancelled:
        return outcome("cancelled")
    if state.failed or res.exit_code != 0:
        text = f"{state.error_text}\n{res.stderr_tail}"
        return outcome("failed", classify_failure(text), claim=state.last_message)
    if state.completed:
        return outcome("completed", claim=state.last_message)
    return outcome(
        "failed", FailureClass.AGENT_FAILURE, note="stream ended without turn.completed\n"
    )


class CodexAdapter(StreamingCliAdapter[StreamState]):
    """Runs ``codex exec --json`` and normalizes its event stream."""

    def __init__(
        self,
        manifest: AdapterManifest | None = None,
        *,
        env_source: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(manifest or load_manifest(), env_source=env_source)

    def build_argv(self, req: AgentRequest) -> list[str]:
        assert self.manifest.binary is not None
        return build_argv(self.manifest.binary, req)

    def new_state(self) -> StreamState:
        return StreamState()

    def parse_line(self, line: str, state: StreamState, now: datetime) -> list[AgentEvent]:
        return parse_line(line, state, now)

    def build_outcome(self, state: StreamState, res: ProcessResult) -> AgentOutcome:
        return build_outcome(state, res)


def create() -> AgentAdapter:
    """Factory used by the registry."""
    return CodexAdapter()
