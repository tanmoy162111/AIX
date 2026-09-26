"""The HTTP API (PLAYBOOK §24): the CLI's services behind FastAPI, no separate orchestration.

Runs execute in the server process on a task group owned by the app lifespan; everything they
record lands in the same event store the CLI reads, so ``aix status`` and ``GET /runs/{id}`` agree.
Stopping the server interrupts in-flight runs; ``aix run --resume <id>`` recovers them.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
import structlog
from anyio.abc import TaskGroup
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from aix.agents.registry import AdapterRegistry
from aix.api.auth import Scope, TokenTable, default_tokens, require
from aix.api.models import (
    ApprovalDecision,
    ApprovalResult,
    CancelResponse,
    RunCreate,
    RunListItem,
    RunStarted,
    RunView,
    TaskView,
)
from aix.config.loader import load_config
from aix.config.schema import AixConfig
from aix.core.orchestrator.approvals import resolve_approval
from aix.core.orchestrator.cancel import request_cancel
from aix.core.orchestrator.executor import RunRequest, execute_run, resume_run
from aix.core.orchestrator.status import snapshot
from aix.domain.enums import ArtifactType
from aix.domain.errors import ConfigError, NoEligibleAgent, ToolFailure
from aix.domain.ids import IdPrefix, new_id
from aix.domain.state import RUN_TERMINAL
from aix.security.approvals import ApprovalRefused, actor, guard_agent_context
from aix.store.db import EventStore
from aix.store.events import ArtifactCreatedPayload

log = structlog.get_logger("aix.api")

POLL_S = 0.1
KEEPALIVE_S = 15.0
SETTLE_S = 0.3
"""Quiet time after a terminal run's manifest artifact before its event stream closes."""
DRAIN_S = 2.0
"""Quiet time after a terminal run with no manifest (never finalized) before the stream closes."""


@dataclass
class ApiState:
    """Per-app services, created by the lifespan."""

    root: Path
    config: AixConfig
    registry: AdapterRegistry
    store: EventStore | None = None
    tasks: TaskGroup | None = None
    background: set[str] = field(default_factory=set[str])
    """Run ids with a job in flight in this process."""


def create_app(
    project_root: Path,
    *,
    tokens: TokenTable | None = None,
    config: AixConfig | None = None,
    registry: AdapterRegistry | None = None,
) -> FastAPI:
    """Build the API for the project at ``project_root``.

    Args:
        tokens: token table; defaults to the config-dir tokens (:func:`default_tokens`).
        config: overrides the layered project config (tests).
        registry: overrides the adapter registry (tests register fake agents here).

    Raises:
        ConfigError: the project config is invalid.
    """
    root = project_root.resolve()
    cfg = config if config is not None else load_config(root).config
    state = ApiState(root=root, config=cfg, registry=registry or AdapterRegistry(cfg))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        (root / ".aix").mkdir(exist_ok=True)
        store = await EventStore.open(root / ".aix" / "aix.db")
        state.store = store
        try:
            async with anyio.create_task_group() as tg:
                state.tasks = tg
                yield
                tg.cancel_scope.cancel()
        finally:
            state.store = None
            state.tasks = None
            await store.close()

    app = FastAPI(title="aix", version="0.1.0", lifespan=lifespan)
    app.state.tokens = dict(tokens) if tokens is not None else default_tokens()
    app.state.api = state
    _routes(app, state)
    return app


def _store(state: ApiState) -> EventStore:
    if state.store is None:  # only outside the lifespan
        raise HTTPException(503, "server is not running")
    return state.store


def _spawn(state: ApiState, run_id: str, job: Any) -> None:
    """Run ``job()`` in the background; a crashing job is logged, never fatal to the server."""
    assert state.tasks is not None

    async def guarded() -> None:
        state.background.add(run_id)
        try:
            await job()
        except Exception:
            log.exception("api.job_failed", run_id=run_id)
        finally:
            state.background.discard(run_id)

    state.tasks.start_soon(guarded)


def _routes(app: FastAPI, state: ApiState) -> None:
    read = Depends(require(Scope.READ))
    run_scope = Depends(require(Scope.RUN))
    approve_scope = Depends(require(Scope.APPROVE))

    @app.get("/runs", dependencies=[read])
    async def list_runs() -> list[RunListItem]:
        return [
            RunListItem(
                run_id=r.id,
                goal=r.goal,
                status=r.status.value,
                created_at=r.created_at.isoformat(),
                finished_at=r.finished_at.isoformat() if r.finished_at else None,
            )
            for r in await _store(state).list_runs()
        ]

    @app.post("/runs", status_code=202, dependencies=[run_scope])
    async def create_run(body: RunCreate) -> RunStarted:
        store = _store(state)
        config = state.config
        if body.budget_usd is not None:
            config = config.model_copy(
                update={
                    "budget": config.budget.model_copy(
                        update={"max_cost_usd_per_run": body.budget_usd}
                    )
                }
            )
        if body.decision_provider is not None:
            config = config.model_copy(
                update={
                    "decision": config.decision.model_copy(
                        update={"provider": body.decision_provider}
                    )
                }
            )
        run_id = new_id(IdPrefix.RUN)
        request = RunRequest(
            project_root=state.root,
            goal=body.goal,
            skill=body.skill,
            allow_dirty=body.allow_dirty,
            max_parallel=body.max_parallel,
            run_id=run_id,
        )
        failure: list[Exception] = []
        finished = anyio.Event()

        async def job() -> None:
            try:
                await execute_run(request, registry=state.registry, store=store, config=config)
            except (ConfigError, ToolFailure, NoEligibleAgent) as exc:
                failure.append(exc)
            finally:
                finished.set()

        _spawn(state, run_id, job)
        # Hand the id out once the run exists; a request rejected before that is an HTTP error.
        while True:
            if await store.get_run(run_id) is not None:
                break
            if finished.is_set():
                exc = failure[0] if failure else RuntimeError("run was not created")
                status = 400 if isinstance(exc, ConfigError) else 409
                raise HTTPException(status if failure else 500, str(exc))
            await anyio.sleep(0.02)
        run = await store.get_run(run_id)
        if run is None:
            raise HTTPException(500, "run was not created")
        return RunStarted(run_id=run_id, status=run.status.value, branch=f"aix/run/{run_id}")

    @app.get("/runs/{run_id}", dependencies=[read])
    async def get_run(run_id: str) -> RunView:
        snap = await _snapshot(state, run_id)
        return RunView(
            run_id=snap.run_id,
            goal=snap.goal,
            status=snap.status.value,
            planner=snap.planner,
            branch=snap.branch,
            counts=snap.counts,
        )

    @app.get("/runs/{run_id}/tasks", dependencies=[read])
    async def get_tasks(run_id: str) -> list[TaskView]:
        snap = await _snapshot(state, run_id)
        return [
            TaskView(
                task_id=t.task_id,
                number=t.number,
                type=t.type,
                title=t.title,
                status=t.status.value,
                agent=t.agent,
                attempts=t.attempts,
                depends_on=t.depends_on,
                failure=t.failure,
            )
            for t in snap.tasks
        ]

    @app.get("/runs/{run_id}/events", dependencies=[read])
    async def stream_events(
        run_id: str,
        follow: bool = True,
        after_seq: int = Query(default=0, ge=0),
        last_event_id: str | None = Header(default=None),
    ) -> StreamingResponse:
        store = _store(state)
        if await store.get_run(run_id) is None:
            raise HTTPException(404, f"unknown run {run_id!r}")
        cursor = after_seq
        if last_event_id is not None:
            try:
                cursor = int(last_event_id)
            except ValueError:
                raise HTTPException(400, "Last-Event-ID must be an event sequence number") from None
        return StreamingResponse(
            _sse(store, run_id, cursor, follow),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/runs/{run_id}/cancel", dependencies=[run_scope])
    async def cancel_run(
        run_id: str, wait_s: float = Query(default=10.0, ge=0.0, le=60.0)
    ) -> CancelResponse:
        result = await request_cancel(_store(state), state.root, run_id, wait_s=wait_s)
        if result.kind == "unknown":
            raise HTTPException(404, f"unknown run {run_id!r}")
        if result.kind == "no_response":
            raise HTTPException(409, f"cancellation requested but the run is still {result.status}")
        return CancelResponse(run_id=run_id, result=result.kind, status=result.status)

    @app.get("/approvals", dependencies=[read])
    async def list_approvals() -> list[dict[str, Any]]:
        pending = await _store(state).list_approvals("pending")
        return [a.model_dump(mode="json") for a in pending]

    @app.post("/approvals/{approval_id}", dependencies=[approve_scope])
    async def decide_approval(approval_id: str, body: ApprovalDecision) -> ApprovalResult:
        store = _store(state)
        try:
            guard_agent_context()
        except ApprovalRefused as exc:
            raise HTTPException(403, str(exc)) from None
        grant = body.decision == "grant"
        try:
            res = await resolve_approval(
                store,
                approval_id,
                grant=grant,
                actor=actor(),
                channel="api_token",
                reason=body.reason,
            )
        except ConfigError as exc:
            raise HTTPException(404, str(exc)) from None
        if grant:
            run_id = res.run_id

            async def resume() -> None:
                await resume_run(
                    run_id,
                    project_root=state.root,
                    registry=state.registry,
                    store=store,
                    config=state.config,
                )

            _spawn(state, run_id, resume)
        return ApprovalResult(
            approval_id=res.approval.id, decision=body.decision, run_id=res.run_id
        )

    @app.get("/artifacts/{artifact_id}", dependencies=[read])
    async def get_artifact(artifact_id: str) -> dict[str, Any]:
        artifact = await _store(state).get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(404, f"unknown artifact {artifact_id!r}")
        doc = artifact.model_dump(mode="json")
        doc["content_url"] = f"/artifacts/{artifact.id}/content"
        return doc

    @app.get("/artifacts/{artifact_id}/content", dependencies=[read])
    async def get_artifact_content(artifact_id: str) -> Response:
        from aix.artifacts.store import ObjectStore

        artifact = await _store(state).get_artifact(artifact_id)
        if artifact is None:
            raise HTTPException(404, f"unknown artifact {artifact_id!r}")
        try:
            data = await ObjectStore(state.root / ".aix" / "artifacts" / "objects").get(
                artifact.sha256
            )
        except FileNotFoundError:
            raise HTTPException(404, "artifact content is missing from the object store") from None
        return Response(
            data, media_type=artifact.media_type, headers={"ETag": f'"{artifact.sha256}"'}
        )


async def _snapshot(state: ApiState, run_id: str):  # type: ignore[no-untyped-def]
    try:
        return await snapshot(_store(state), run_id)
    except ConfigError:
        raise HTTPException(404, f"unknown run {run_id!r}") from None


async def _sse(store: EventStore, run_id: str, after_seq: int, follow: bool) -> AsyncIterator[str]:
    """Server-sent events for a run: backlog first, then live until the run is finished.

    A run is finished once it is terminal and its manifest artifact (written last, after the
    terminal event) has been sent, or when nothing new arrives for ``DRAIN_S``.
    """
    cursor = after_seq
    quiet = 0.0
    keepalive = 0.0
    manifest = False
    while True:
        run = await store.get_run(run_id)
        terminal = run is not None and run.status in RUN_TERMINAL
        events = await store.events(run_id=run_id, after_seq=cursor)
        for event in events:
            cursor = event.seq
            payload = event.payload
            if isinstance(payload, ArtifactCreatedPayload):
                manifest = manifest or payload.artifact.type is ArtifactType.MANIFEST
            yield (
                f"id: {event.seq}\nevent: {event.type}\n"
                f"data: {json.dumps(event.model_dump(mode='json', serialize_as_any=True))}\n\n"
            )
        if not follow:
            return
        if events:
            quiet = keepalive = 0.0
            continue
        if terminal and quiet >= (SETTLE_S if manifest else DRAIN_S):
            return
        await anyio.sleep(POLL_S)
        quiet += POLL_S
        keepalive += POLL_S
        if keepalive >= KEEPALIVE_S:
            keepalive = 0.0
            yield ": keep-alive\n\n"


__all__ = ["ApiState", "create_app"]
