"""Live checks against the real OpenCode CLI. Skipped unless AIX_LIVE=1 (may cost money)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import repos
from aix.agents.adapters.opencode import OpenCodeAdapter
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.enums import FailureClass
from aix.domain.ids import IdPrefix, new_id

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def _need_opencode() -> None:
    if shutil.which("opencode") is None:
        pytest.skip("opencode CLI not installed")


async def test_probe_finds_all_required_flags() -> None:
    spec = await OpenCodeAdapter().probe()
    assert spec.health == "ready", spec.health_reason


async def test_tiny_read_only_prompt(tmp_path: Path) -> None:
    ws = repos.materialize_sample_py(tmp_path / "ws")
    adapter = OpenCodeAdapter()
    req = AgentRequest(
        attempt_id=new_id(IdPrefix.ATTEMPT),
        workspace=ws,
        prompt="Reply with the single word OK. Do not use any tools or run any commands.",
        timeout_s=180,
        permissions=AgentPermissions(read_only=True),
    )
    h = await adapter.start(req)
    async for _ in adapter.events(h):
        pass
    out = await adapter.wait(h)
    if out.failure is FailureClass.AUTH_FAILURE:
        pytest.skip(f"opencode CLI not authenticated for this account: {out.stderr_tail[:200]}")
    assert out.status == "completed", out.stderr_tail
    assert out.claim and "OK" in out.claim
    assert repos.git(ws, "status", "--porcelain").stdout == ""
