"""Live checks against a local Ollama daemon. Skipped unless AIX_LIVE=1 (free: runs locally)."""

from __future__ import annotations

from pathlib import Path

import pytest

import repos
from aix.agents.adapters.ollama import OllamaAdapter
from aix.agents.protocol import AgentPermissions, AgentRequest
from aix.domain.ids import IdPrefix, new_id

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def _adapter() -> OllamaAdapter:
    adapter = OllamaAdapter()
    spec = await adapter.probe()
    if spec.health != "ready":
        pytest.skip(f"ollama not usable: {spec.health_reason}")
    return adapter


async def test_tiny_read_only_prompt(tmp_path: Path) -> None:
    adapter = await _adapter()
    ws = repos.materialize_sample_py(tmp_path / "ws")
    req = AgentRequest(
        attempt_id=new_id(IdPrefix.ATTEMPT),
        workspace=ws,
        prompt="Reply with the single word OK.",
        timeout_s=180,
        permissions=AgentPermissions(read_only=True),
    )
    h = await adapter.start(req)
    async for _ in adapter.events(h):
        pass
    out = await adapter.wait(h)
    assert out.status == "completed", out.stderr_tail
    assert out.claim  # a 0.5B model may not obey "single word"; only require an answer
    assert repos.git(ws, "status", "--porcelain").stdout == ""
