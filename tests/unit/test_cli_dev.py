from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

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
