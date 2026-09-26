"""Discovery and deterministic rendering of JSON Schemas for domain models (PLAYBOOK §6).

Pure: no file I/O. Writing lives in the CLI (`aix dev export-schemas`).
"""

from __future__ import annotations

import importlib
import inspect
import json
import re
from typing import Any

from pydantic import BaseModel

from aix.domain.base import DomainModel

_MODULES = ("agents", "artifacts", "decisions", "execution", "runs", "tasks", "verification")


def schema_name(class_name: str) -> str:
    """``TaskGraph`` -> ``task_graph``."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()


def collect_domain_models() -> dict[str, type[BaseModel]]:
    """Every ``DomainModel`` subclass defined in ``aix.domain.*`` keyed by snake_case name."""
    found: dict[str, type[BaseModel]] = {}
    for mod_name in _MODULES:
        module = importlib.import_module(f"aix.domain.{mod_name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, DomainModel)
                and obj is not DomainModel
                and obj.__module__ == module.__name__
            ):
                found[schema_name(obj.__name__)] = obj
    return dict(sorted(found.items()))


def render_schema(model: type[BaseModel]) -> str:
    """JSON Schema for ``model`` with sorted keys, 2-space indent and a trailing newline."""
    doc: dict[str, Any] = model.model_json_schema()
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"
