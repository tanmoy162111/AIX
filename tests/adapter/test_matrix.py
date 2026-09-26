"""The §10.5 adapter test matrix, run identically against every adapter."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from adapter_matrix import MATRIX_ROWS, NOT_APPLICABLE, Rig
from aix.agents.protocol import AgentOutcome, AgentPermissions
from aix.domain.enums import FailureClass
from aix.domain.errors import UnsupportedError

pytestmark = pytest.mark.anyio

KINDS = ["fake", "claude", "codex", "gemini"]


@pytest.fixture(params=KINDS)
def rig(request: pytest.FixtureRequest, tmp_path: Path) -> Rig:
    return Rig(request.param, tmp_path)


async def run_scenario(rig: Rig, scenario: str) -> tuple[list[str], AgentOutcome]:
    case = rig.build(scenario)
    h = await case.adapter.start(case.req)
    kinds = [e.kind async for e in case.adapter.events(h)]
    return kinds, await case.adapter.wait(h)


# ---- the matrix ---------------------------------------------------------------------------


async def test_probe(rig: Rig) -> None:
    ready = await rig.probe_ready().probe()
    assert ready.health == "ready" and ready.id == rig.kind
    gone = await rig.probe_missing().probe()
    assert gone.health == "unavailable" and gone.health_reason


async def test_success(rig: Rig) -> None:
    kinds, out = await run_scenario(rig, "success")
    assert out.status == "completed" and out.failure is None
    assert out.claim and kinds[0] == "started" and kinds[-1] == "finished"


async def test_nonzero_exit(rig: Rig) -> None:
    _, out = await run_scenario(rig, "nonzero_exit")
    assert out.status == "failed" and out.exit_code == 2
    assert out.failure is FailureClass.AGENT_FAILURE
    assert "crash" in out.stderr_tail


async def test_malformed_stream_line(rig: Rig) -> None:
    if (rig.kind, "malformed_stream_line") in NOT_APPLICABLE:
        pytest.skip(NOT_APPLICABLE[(rig.kind, "malformed_stream_line")])
    kinds, out = await run_scenario(rig, "malformed_stream_line")
    assert kinds.count("error") == 3 and out.status == "completed"


async def test_timeout(rig: Rig) -> None:
    _, out = await run_scenario(rig, "timeout")
    assert out.status == "timeout" and out.failure is FailureClass.TIMEOUT


async def test_cancellation(rig: Rig) -> None:
    case = rig.build("cancellation")
    async with anyio.create_task_group() as tg:  # opened before start() so scopes nest
        h = await case.adapter.start(case.req)

        async def cancel_soon() -> None:
            await anyio.sleep(0.4)
            await case.adapter.cancel(h, grace_s=1)

        tg.start_soon(cancel_soon)
        out = await case.adapter.wait(h)
    assert out.status == "cancelled" and out.failure is None


async def test_auth_failure_classification(rig: Rig) -> None:
    _, out = await run_scenario(rig, "auth_failure")
    assert out.status == "failed" and out.failure is FailureClass.AUTH_FAILURE


async def test_rate_limit_classification(rig: Rig) -> None:
    _, out = await run_scenario(rig, "rate_limit")
    assert out.status == "failed" and out.failure is FailureClass.RATE_LIMITED


async def test_usage_parsing(rig: Rig) -> None:
    _, out = await run_scenario(rig, "usage_parsing")
    assert (out.usage.input_tokens or 0) > 0 and (out.usage.output_tokens or 0) > 0
    if rig.kind in ("fake", "claude"):
        assert out.usage.cost_usd is not None  # reported cost
    else:
        assert out.usage.cost_usd is None  # codex and gemini report tokens only


async def test_session_resume(rig: Rig) -> None:
    case = rig.build("session_resume")
    if rig.kind == "fake":
        with pytest.raises(UnsupportedError):
            await case.adapter.resume("s-1", case.req)
        return
    h = await case.adapter.resume("s-1", case.req)
    out = await case.adapter.wait(h)
    assert out.status == "completed" and "s-1" in rig.argv()


async def test_permission_translation(rig: Rig) -> None:
    if (rig.kind, "permission_translation") in NOT_APPLICABLE:
        pytest.skip(NOT_APPLICABLE[(rig.kind, "permission_translation")])
    case = rig.build("success")
    req = case.req.model_copy(update={"permissions": AgentPermissions(read_only=True)})
    h = await case.adapter.start(req)
    await case.adapter.wait(h)
    argv = rig.argv()
    if rig.kind == "claude":
        assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
        assert "Edit" in argv[argv.index("--disallowedTools") + 1]
    elif rig.kind == "gemini":
        assert argv[argv.index("--approval-mode") + 1] == "plan"
    else:
        assert argv[argv.index("-s") + 1] == "read-only"


# ---- guard: the matrix itself must stay complete ---------------------------------------------


def test_matrix_rows_match_the_spec() -> None:
    assert MATRIX_ROWS == (
        "probe", "success", "nonzero_exit", "malformed_stream_line", "timeout", "cancellation",
        "auth_failure", "rate_limit", "usage_parsing", "session_resume", "permission_translation",
    )  # fmt: skip


def test_every_row_has_a_test_and_every_skip_has_a_reason() -> None:
    names = {n.removeprefix("test_") for n in globals() if n.startswith("test_")}
    for row in MATRIX_ROWS:
        expected = {
            "auth_failure": "auth_failure_classification",
            "rate_limit": "rate_limit_classification",
        }
        assert expected.get(row, row) in names, f"no matrix test for {row}"
    for (kind, row), reason in NOT_APPLICABLE.items():
        assert kind in KINDS and row in MATRIX_ROWS and reason
