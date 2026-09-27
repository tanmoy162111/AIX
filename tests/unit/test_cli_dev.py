from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aix.agents.protocol import AgentHandle, AgentRequest
from aix.cli.main import app

runner = CliRunner()


def test_export_schemas_writes_files_and_removes_stale(tmp_path: Path) -> None:
    out = tmp_path / "schemas"
    out.mkdir()
    (out / "stale.json").write_text("{}")
    result = runner.invoke(app, ["dev", "export-schemas", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert not (out / "stale.json").exists()
    run_schema = json.loads((out / "run.json").read_text())
    assert run_schema["title"] == "Run"


def test_export_schemas_is_idempotent(tmp_path: Path) -> None:
    out = tmp_path / "s"
    runner.invoke(app, ["dev", "export-schemas", "--out", str(out)])
    first = {p.name: p.read_text() for p in out.glob("*.json")}
    runner.invoke(app, ["dev", "export-schemas", "--out", str(out)])
    assert first == {p.name: p.read_text() for p in out.glob("*.json")}


def test_rebuild_projections_command(tmp_path: Path) -> None:
    import anyio

    import scenarios
    from aix.store.db import EventStore

    db = tmp_path / "aix.db"

    async def seed() -> tuple[int, list[tuple[object, ...]]]:
        store = await EventStore.open(db)
        try:
            await scenarios.play_full_run(store)
            await store.execute_raw("DELETE FROM runs")
            return await store.count_events(), await store.dump_table("runs")
        finally:
            await store.close()

    n, broken = anyio.run(seed)
    assert broken == []
    result = runner.invoke(app, ["dev", "rebuild-projections", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert f"replayed {n} events" in result.output

    async def runs() -> int:
        store = await EventStore.open(db)
        try:
            return len(await store.list_runs())
        finally:
            await store.close()

    assert anyio.run(runs) == 1


def test_rebuild_projections_without_db_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dev", "rebuild-projections", "--db", str(tmp_path / "nope.db")])
    assert result.exit_code == 2


def test_record_refuses_without_aix_live(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIX_LIVE", raising=False)
    result = runner.invoke(app, ["dev", "record", "claude", "--out", str(tmp_path)])
    assert result.exit_code == 2
    assert "AIX_LIVE=1" in result.output
    assert not list(tmp_path.iterdir())


@pytest.mark.anyio
async def test_record_read_only_mirrors_stream_in_scratch_repo(tmp_path: Path) -> None:
    from aix.agents.adapters.fake import FakeAdapter
    from aix.agents.record import RECORD_FILE, record_read_only

    seen: list[Path] = []

    class Spy(FakeAdapter):
        async def start(self, req: AgentRequest) -> AgentHandle:
            seen.append(req.workspace)
            assert req.permissions.read_only
            assert req.stream_path == tmp_path / "out" / RECORD_FILE
            assert (req.workspace / ".git").exists()
            assert req.stream_path is not None
            req.stream_path.write_text('{"type":"result"}\n')
            return await super().start(req)

    path, outcome = await record_read_only(Spy(), tmp_path / "out")
    assert path.read_text() == '{"type":"result"}\n'
    assert outcome.status in {"completed", "failed"}
    assert not seen[0].exists()  # scratch repo is removed


def test_sanitize_recording_drops_noise_and_home(tmp_path: Path) -> None:
    from aix.agents.record import sanitize_recording

    rec = tmp_path / "r.jsonl"
    rec.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "hook_response", "output": "secret"}),
                json.dumps({"type": "system", "subtype": "init", "skills": ["a"], "model": "m"}),
                json.dumps({"type": "result", "path": "/h/me/x"}),
                "not json /h/me/y",
            ]
        )
    )
    sanitize_recording(rec, home=Path("/h/me"))
    lines = rec.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0]) == {"type": "system", "subtype": "init", "skills": [], "model": "m"}
    assert json.loads(lines[1])["path"] == "~/x"
    assert lines[2] == "not json ~/y"
