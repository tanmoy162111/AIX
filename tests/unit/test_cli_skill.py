from __future__ import annotations

import json

from typer.testing import CliRunner

from aix.cli.main import app

runner = CliRunner()


def test_skill_list_json() -> None:
    r = runner.invoke(app, ["skill", "list", "--json"])
    assert r.exit_code == 0, r.output
    names = [s["name"] for s in json.loads(r.output)]
    assert "feature-implementation" in names and len(names) == 7


def test_skill_list_table() -> None:
    r = runner.invoke(app, ["skill", "list"])
    assert r.exit_code == 0 and "bugfix" in r.output


def test_skill_inspect() -> None:
    r = runner.invoke(app, ["skill", "inspect", "bugfix", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.output)
    assert doc["meta"]["name"] == "bugfix" and doc["workflow"]["steps"]
    assert doc["instructions"]


def test_skill_inspect_unknown_fails_cleanly() -> None:
    r = runner.invoke(app, ["skill", "inspect", "nope"])
    assert r.exit_code != 0 and "unknown skill" in r.output
