from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic import ValidationError

import factories as f
from aix.agents.protocol import (
    AgentAdapter,
    AgentEvent,
    AgentHandle,
    AgentOutcome,
    AgentPermissions,
    AgentRequest,
)
from aix.domain.agents import AgentSpec
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id


def _request(**kw: object) -> AgentRequest:
    data: dict[str, object] = {
        "attempt_id": new_id(IdPrefix.ATTEMPT),
        "workspace": Path("/tmp/ws"),
        "prompt": "do it",
        "timeout_s": 60,
        "permissions": AgentPermissions(),
    }
    data.update(kw)
    return AgentRequest.model_validate(data)


def test_request_defaults_and_validation() -> None:
    r = _request()
    assert r.model is None and r.env == {} and r.session_ref is None
    with pytest.raises(ValidationError):
        _request(timeout_s=0)
    with pytest.raises(ValidationError):
        _request(attempt_id="nope")


def test_permissions_default_to_least_privilege_shape() -> None:
    p = AgentPermissions()
    assert p.read_only is False and p.network == "provider_default"
    assert p.allowed_tools is None and p.write_scope == []


def test_event_kinds_are_restricted() -> None:
    for kind in ("started", "text", "tool_call", "tool_result", "usage", "error", "finished"):
        assert AgentEvent(kind=kind, ts=f.NOW).kind == kind  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AgentEvent(kind="bogus", ts=f.NOW)  # type: ignore[arg-type]


def test_outcome_claim_is_optional_and_failure_typed() -> None:
    o = AgentOutcome(exit_code=1, status="failed", failure=FailureClass.AGENT_FAILURE)
    assert o.claim is None and o.usage.cost_usd is None and o.stderr_tail == ""


class _Dummy:
    id = "dummy"

    async def probe(self) -> AgentSpec:
        return f.agent_spec(id="dummy")

    async def start(self, req: AgentRequest) -> AgentHandle:
        return AgentHandle(attempt_id=req.attempt_id, adapter_id=self.id)

    def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        async def gen() -> AsyncIterator[AgentEvent]:
            yield AgentEvent(kind="started", ts=f.NOW)

        return gen()

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        return AgentOutcome(exit_code=0, status="completed")

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None: ...

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        return await self.start(req)


def test_protocol_is_structural() -> None:
    assert isinstance(_Dummy(), AgentAdapter)
    assert not isinstance(object(), AgentAdapter)
