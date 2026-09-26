"""Standard artifacts of a run (PLAYBOOK §21.3), built from the event store and run directory."""

from __future__ import annotations

import json
import mimetypes
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
from pydantic import BaseModel

from aix.artifacts.store import ArtifactWriter
from aix.domain.artifacts import Artifact, Producer
from aix.domain.enums import ArtifactType
from aix.domain.execution import Attempt
from aix.store import events as ev
from aix.store.db import EventStore

_CONTROL: Final = Producer(kind="control_plane", id="aix")


@dataclass(frozen=True)
class ManifestEntry:
    """One named artifact in a run's manifest."""

    name: str
    artifact_id: str
    type: str
    sha256: str
    size: int


def dumps(obj: object) -> bytes:
    """Deterministic JSON: sorted keys, 2-space indent, trailing newline."""
    return (json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _dump(model: BaseModel) -> object:
    return model.model_dump(mode="json")


def _verification_type(path: Path) -> tuple[ArtifactType, str]:
    name = path.name.lower()
    if name.endswith(".xml"):
        return ArtifactType.JUNIT, "application/xml"
    if name.endswith((".sarif", ".sarif.json")):
        return ArtifactType.SARIF, "application/json"
    media = mimetypes.guess_type(name)[0] or "text/plain"
    return ArtifactType.LOG, media if media.startswith(
        ("text/", "application/json")
    ) else "text/plain"


async def _read(path: Path) -> bytes | None:
    ap = anyio.Path(path)
    return await ap.read_bytes() if await ap.is_file() else None


async def write_standard_artifacts(
    store: EventStore, writer: ArtifactWriter, run_id: str, *, runs_dir: Path
) -> list[ManifestEntry]:
    """Write plan, patches, verification, prompts, streams, decision log and agent trace.

    ``runs_dir`` is ``.aix/runs/<run_id>``. Entries come back in a fixed order (plan, then per
    task in creation order and per attempt by number, then decision log and agent trace) so the
    manifest is reproducible. Missing files (for example no patch for a read-only task) are simply
    absent. The manifest itself is written by :func:`write_manifest` once everything else exists.
    """
    entries: list[ManifestEntry] = []

    async def add(
        name: str,
        type_: ArtifactType,
        data: bytes,
        media: str,
        producer: Producer = _CONTROL,
        *,
        task_id: str | None = None,
        attempt_id: str | None = None,
    ) -> Artifact:
        art = await writer.write(
            type_, data, media, producer=producer, task_id=task_id, attempt_id=attempt_id
        )
        entries.append(ManifestEntry(name, art.id, art.type.value, art.sha256, art.size))
        return art

    planned = await store.events(run_id=run_id, types=["run.planned"])
    if planned:
        p = planned[0].payload
        assert isinstance(p, ev.RunPlannedPayload)
        await add(
            "plan.json",
            ArtifactType.PLAN,
            dumps(
                {
                    "intent": _dump(p.intent),
                    "graph": _dump(p.graph),
                    "planner": p.planner,
                    "warnings": p.warnings,
                }
            ),
            "application/json",
        )

    trace: list[dict[str, object]] = []
    for task in await store.get_tasks(run_id):
        attempts: Sequence[Attempt] = await store.get_attempts(task.id)
        last_patch: tuple[str, Attempt, bytes] | None = None
        for attempt in attempts:
            agent = Producer(kind="agent", id=attempt.agent_id, model=attempt.model)
            result = await store.get_result(attempt.id)
            trace.append(
                {
                    "task_id": task.id,
                    "attempt_id": attempt.id,
                    "number": attempt.number,
                    "agent_id": attempt.agent_id,
                    "model": attempt.model,
                    "mutation": attempt.mutation.value if attempt.mutation else None,
                    "status": result.status if result else None,
                    "failure": result.failure.value if result and result.failure else None,
                    "exit_code": result.exit_code if result else None,
                    "duration_ms": result.duration_ms if result else None,
                    "usage": _dump(result.usage) if result else None,
                }
            )
            if (prompt := await _read(runs_dir / f"{attempt.id}.prompt.txt")) is not None:
                await add(
                    f"prompt/{attempt.id}.txt", ArtifactType.PROMPT, prompt, "text/plain", agent,
                    task_id=task.id, attempt_id=attempt.id,
                )  # fmt: skip
            if (stream := await _read(runs_dir / f"{attempt.id}.stream.jsonl")) is not None:
                await add(
                    f"stream/{attempt.id}.jsonl", ArtifactType.STREAM, stream,
                    "application/x-ndjson", agent, task_id=task.id, attempt_id=attempt.id,
                )  # fmt: skip
            if (patch := await _read(runs_dir / f"{attempt.id}.patch")) is not None:
                last_patch = (attempt.id, attempt, patch)
            if (report := await store.get_verification(attempt.id)) is not None:
                await add(
                    f"verification/{attempt.id}.json", ArtifactType.VERIFICATION,
                    dumps(_dump(report)), "application/json",
                    task_id=task.id, attempt_id=attempt.id,
                )  # fmt: skip
                vdir = anyio.Path(runs_dir / attempt.id / "verification")
                if await vdir.is_dir():
                    files = sorted([f async for f in vdir.rglob("*") if await f.is_file()])
                    for f in files:
                        rel = Path(f).relative_to(runs_dir / attempt.id / "verification")
                        vtype, media = _verification_type(Path(f))
                        await add(
                            f"verification/{attempt.id}/{rel.as_posix()}", vtype,
                            await f.read_bytes(), media, Producer(kind="check", id="verification"),
                            task_id=task.id, attempt_id=attempt.id,
                        )  # fmt: skip
        if last_patch is not None:
            _, attempt, patch = last_patch
            await add(
                f"patch/{task.id}.diff", ArtifactType.PATCH, patch, "text/x-diff",
                Producer(kind="agent", id=attempt.agent_id, model=attempt.model),
                task_id=task.id, attempt_id=attempt.id,
            )  # fmt: skip

    decisions = [_dump(d) for d in await store.get_decisions(run_id)]
    await add("decision-log.json", ArtifactType.DECISION_LOG, dumps(decisions), "application/json")
    await add("agent-trace.json", ArtifactType.AGENT_TRACE, dumps(trace), "application/json")
    return entries


async def write_named(
    writer: ArtifactWriter, name: str, type_: ArtifactType, data: bytes, media: str
) -> ManifestEntry:
    """Write one control-plane artifact and return its manifest entry."""
    art = await writer.write(type_, data, media, producer=_CONTROL)
    return ManifestEntry(name, art.id, art.type.value, art.sha256, art.size)


async def write_manifest(
    writer: ArtifactWriter, run_id: str, entries: Sequence[ManifestEntry]
) -> Artifact:
    """Write ``manifest.json`` listing every artifact with its hash (§21.3).

    The manifest cannot contain its own hash; that is the ``sha256`` of the returned artifact,
    and ``aix artifact verify`` (M7.4) re-derives it.
    """
    doc = {
        "run_id": run_id,
        "artifacts": [
            {
                "name": e.name,
                "artifact_id": e.artifact_id,
                "type": e.type,
                "sha256": e.sha256,
                "size": e.size,
            }
            for e in entries
        ],
    }
    return await writer.write(
        ArtifactType.MANIFEST,
        dumps(doc),
        "application/json",
        producer=_CONTROL,
        inputs=[e.artifact_id for e in entries],
    )
