"""Committed JSON Schemas must equal freshly generated ones (PLAYBOOK §6)."""

from __future__ import annotations

from pathlib import Path

from aix.cli.dev import all_schema_models
from aix.domain.schemas import render_schema

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"


def test_committed_schemas_match_models() -> None:
    expected = {name: render_schema(model) for name, model in all_schema_models().items()}
    committed = {p.stem: p.read_text() for p in SCHEMAS.glob("*.json")}
    assert set(committed) == set(expected), "run `uv run aix dev export-schemas`"
    for name, text in expected.items():
        assert committed[name] == text, f"schemas/{name}.json drifted; re-export"
