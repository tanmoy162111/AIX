from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aix.artifacts.store import ArtifactWriter, ObjectStore
from aix.config.schema import AixConfig
from aix.core.orchestrator.recorder import RunRecorder, new_run
from aix.domain.artifacts import Producer
from aix.domain.enums import ArtifactType
from aix.domain.ids import IdPrefix, new_id
from aix.store.db import EventStore

pytestmark = pytest.mark.anyio


async def test_put_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "objects")
    a = await store.put(b"hello")
    b = await store.put(b"hello")
    assert a == b and a.sha256 == hashlib.sha256(b"hello").hexdigest() and a.size == 5
    assert len([p for p in (tmp_path / "objects").rglob("*") if p.is_file()]) == 1
    assert await store.get(a.sha256) == b"hello" and await store.exists(a.sha256)
    assert (tmp_path / "objects" / a.relpath).read_bytes() == b"hello"


async def test_missing_and_tampered_blobs(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "objects")
    blob = await store.put(b"data")
    assert await store.verify(blob.sha256)
    (tmp_path / "objects" / blob.relpath).write_bytes(b"evil")
    assert not await store.verify(blob.sha256)
    assert not await store.verify("0" * 64) and not await store.exists("0" * 64)
    with pytest.raises(FileNotFoundError):
        await store.get("0" * 64)


async def test_put_repairs_a_tampered_blob(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "objects")
    blob = await store.put(b"data")
    (tmp_path / "objects" / blob.relpath).write_bytes(b"evil")
    await store.put(b"data")
    assert await store.verify(blob.sha256)


async def test_rejects_malformed_hashes(tmp_path: Path) -> None:
    store = ObjectStore(tmp_path / "objects")
    for bad in ("../../etc/passwd", "abc", "Z" * 64):
        assert not await store.exists(bad)
        with pytest.raises(ValueError):
            await store.get(bad)


async def test_writer_records_event_with_provenance_and_redacts(tmp_path: Path) -> None:
    db = await EventStore.open(tmp_path / "aix.db")
    try:
        run_id = new_id(IdPrefix.RUN)
        now = lambda: datetime.now(UTC)  # noqa: E731
        rec = RunRecorder(db, new_run(run_id, tmp_path, "g", AixConfig(), now()), now)
        await rec.start()
        writer = ArtifactWriter(ObjectStore(tmp_path / "objects"), rec, aix_version="1.2.3")
        task_id = new_id(IdPrefix.TASK)
        text = 'log\napi_key = "abcdefghijklmnop1234567890"\n'
        art = await writer.write(
            ArtifactType.LOG, text.encode(), "text/plain",
            producer=Producer(kind="check", id="pytest"), task_id=task_id,
            tools=["pytest 8"], base_commit="a" * 40,
        )  # fmt: skip
        again = await writer.write(
            ArtifactType.LOG, text.encode(), "text/plain", producer=Producer(kind="check", id="x")
        )
        assert art.sha256 == again.sha256 and art.id != again.id
        blob = await ObjectStore(tmp_path / "objects").get(art.sha256)
        assert b"abcdefghijklmnop1234567890" not in blob and b"[REDACTED]" in blob
        assert art.provenance.aix_version == "1.2.3" and art.provenance.task_id == task_id
        assert art.provenance.run_id == run_id and art.size == len(blob)
        stored = await db.get_artifacts(run_id)
        assert [a.id for a in stored] == [art.id, again.id]
    finally:
        await db.close()


async def test_binary_media_types_are_not_redacted(tmp_path: Path) -> None:
    db = await EventStore.open(tmp_path / "aix.db")
    try:
        run_id = new_id(IdPrefix.RUN)
        now = lambda: datetime.now(UTC)  # noqa: E731
        rec = RunRecorder(db, new_run(run_id, tmp_path, "g", AixConfig(), now()), now)
        await rec.start()
        writer = ArtifactWriter(ObjectStore(tmp_path / "objects"), rec, aix_version="1")
        raw = bytes(range(256))
        art = await writer.write(
            ArtifactType.OTHER, raw, "application/octet-stream",
            producer=Producer(kind="control_plane", id="aix"),
        )  # fmt: skip
        assert await ObjectStore(tmp_path / "objects").get(art.sha256) == raw
    finally:
        await db.close()


def test_sanitize_stream_strips_inventories_hooks_and_home() -> None:
    import json
    from pathlib import Path

    from aix.artifacts.sanitize import sanitize_stream, sanitize_stream_bytes

    home = Path("/h/me")
    lines = [
        {"type": "system", "subtype": "hook_response", "output": "secret"},
        {"type": "system", "subtype": "commands_changed", "commands": ["x"] * 50},
        {
            "type": "system",
            "subtype": "init",
            "plugins": [{"path": "/h/me/.claude/p"}],
            "model": "m",
        },
        {"type": "result", "path": "/h/me/x"},
    ]
    text = "\n".join([*map(json.dumps, lines), "garbage /h/me/y"])
    out = sanitize_stream(text, home)
    assert "/h/me" not in out and "secret" not in out and "commands" not in out
    rows = out.splitlines()
    assert json.loads(rows[0])["plugins"] == [] and json.loads(rows[0])["model"] == "m"
    assert json.loads(rows[1])["path"] == "~/x" and rows[2] == "garbage ~/y"
    assert sanitize_stream(out, home) == out  # idempotent
    assert sanitize_stream("", home) == ""
    assert sanitize_stream_bytes(text.encode(), home) == out.encode()
