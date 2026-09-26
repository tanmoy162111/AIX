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
