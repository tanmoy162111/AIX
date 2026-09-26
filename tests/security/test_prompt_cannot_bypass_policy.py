"""A scripted agent that tries to defeat policy is denied and recorded (M8.5, PLAYBOOK §20.4).

The fake agent runs real commands with the real agent environment: `aix approve` with the correct
token, reading the token file, plus writes outside its scope and into `.aix/`.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.adapters.fake.script import FakeMatch, FakeScript, FakeStep
from aix.agents.fakes import make_fake_entry
from aix.agents.registry import AdapterRegistry
from aix.core.orchestrator.executor import execute_graph
from aix.core.orchestrator.plan import record_plan
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.decisions import Approval
from aix.domain.enums import Capability, FailureClass, RunStatus, TaskType
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import Intent, Task, TaskGraph, VerificationSpec
from aix.security.approvals import ensure_token, token_path
from aix.store import events as ev
from aix.store.db import EventStore
from verif_env import fast_config

pytestmark = pytest.mark.anyio


async def test_agent_cannot_approve_read_the_token_or_touch_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    token = ensure_token()
    repo = repos.materialize_sample_py(tmp_path / "proj")
    (repo / ".aix").mkdir()
    cfg = fast_config()
    store = await EventStore.open(repo / ".aix" / "aix.db")
    now = lambda: datetime.now(UTC)  # noqa: E731
    wm = WorkspaceManager(repo)
    run_id = new_id(IdPrefix.RUN)
    await wm.create_run_branch(run_id)
    rec = RunRecorder(store, new_run(run_id, wm.root, "goal", cfg, now()), now)
    await rec.start()
    task = Task(
        id=new_id(IdPrefix.TASK), run_id=run_id, title="t", goal="do it", type=TaskType.IMPLEMENT,
        required_capabilities=[Capability.IMPLEMENT], file_scope=["a.py"],
        verification=VerificationSpec(required=[K.BUILD]),
    )  # fmt: skip
    graph = TaskGraph(id=new_id(IdPrefix.GRAPH), run_id=run_id, tasks=[task])
    await record_plan(rec, Intent(goal="g", kind="coding", risk="low"), graph, "test", [])
    approval = Approval(
        id=new_id(IdPrefix.APPROVAL), subject=task.id, action="deploy", requested_at=now()
    )
    await rec.emit("approval.requested", ev.ApprovalRequestedPayload(approval=approval))

    approve = [
        sys.executable, "-c", "from aix.cli.main import app; app()",
        "approve", approval.id, "--token", token, "--project", str(repo),
    ]  # fmt: skip
    hostile = FakeStep(
        write_files={"a.py": "x = 1\n"},
        write_outside_scope=["other.py", ".aix/evil.txt"],
        run_commands=[approve, ["cat", str(token_path())], ["cat", ".aix/aix.db"]],
        claim="All done, approved myself.",
    )
    registry = AdapterRegistry(cfg, builtin_ids=())
    registry.register(
        make_fake_entry(
            "fake-a",
            scripts=[FakeScript(match=FakeMatch(task_type="implement"), attempts=[hostile])],
            capabilities={Capability.IMPLEMENT: 0.9},
        )
    )  # fmt: skip
    try:
        with anyio.fail_after(180):
            outcome = await execute_graph(
                rec, wm, graph, registry=registry, config=cfg, backoff_scale=0
            )
        # the policy failure is not retried; the ladder ends at a human, so the run waits for one
        assert outcome.status is RunStatus.WAITING_APPROVAL
        assert outcome.tasks[0].failure is FailureClass.POLICY_FAILURE

        # denied: the agent's target approval is untouched and nothing was ever granted; the only
        # other approval is the control plane's own request for a human to look at the task
        approvals = await store.list_approvals()
        mine = next(a for a in approvals if a.id == approval.id)
        assert mine.status == "pending" and mine.actor is None
        assert all(a.status == "pending" for a in approvals) and len(approvals) == 2
        assert await store.events(run_id=run_id, types=["approval.granted"]) == []

        # recorded: every attempt at bypassing policy is an event
        kinds = {
            e.payload.kind
            for e in await store.events(run_id=run_id, types=["policy.violation"])
            if isinstance(e.payload, ev.PolicyViolationPayload)
        }
        assert {"approval_bypass", "secret_path", "control_plane_state", "scope"} <= kinds

        # the guard itself said no, and named the reason
        stream = next((repo / ".aix" / "runs" / run_id).glob("*.stream.jsonl")).read_text()
        assert "approvals cannot be granted from inside an agent context" in stream
        assert token not in stream  # even the stream copy of the token read is redacted
        # the hostile change never reached the run branch
        shown = repos.git(repo, "show", f"aix/run/{run_id}:a.py", check=False)
        assert shown.returncode != 0
    finally:
        await store.close()
