"""Live checks against the real Gemini CLI. Skipped unless AIX_LIVE=1 (may cost money)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import repos
from aix.agents.adapters.gemini import GeminiAdapter
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.ids import IdPrefix, new_id

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(autouse=True)
def _need_gemini() -> None:
    if shutil.which("gemini") is None:
        pytest.skip("gemini CLI not installed")


async def test_probe_finds_all_required_flags() -> None:
    spec = await GeminiAdapter().probe()
    assert spec.health == "ready", spec.health_reason


async def test_tiny_read_only_prompt(tmp_path: Path) -> None:
    ws = repos.materialize_sample_py(tmp_path / "ws")
    adapter = GeminiAdapter()
    req = AgentRequest(
        attempt_id=new_id(IdPrefix.ATTEMPT),
        workspace=ws,
        prompt="Reply with the single word OK. Do not use any tools.",
        timeout_s=180,
        permissions=AgentPermissions(read_only=True),
    )
    h = await adapter.start(req)
    async for _ in adapter.events(h):
        pass
    out = await adapter.wait(h)
    assert out.status == "completed", out.stderr_tail
    assert out.claim and "OK" in out.claim
    assert repos.git(ws, "status", "--porcelain").stdout == ""
