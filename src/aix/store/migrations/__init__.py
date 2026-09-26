"""Ordered SQL migrations (``NNN_name.sql``), applied by ``PRAGMA user_version``.

Each file is a self-contained script that starts with ``BEGIN;`` and ends with
``PRAGMA user_version = N; COMMIT;`` so a failed migration leaves the database untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

_NAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def all_migrations() -> list[Migration]:
    """All bundled migrations sorted by version; versions must be contiguous from 1."""
    found: list[Migration] = []
    for entry in resources.files(__package__).iterdir():
        m = _NAME.match(entry.name)
        if m:
            found.append(Migration(int(m.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort(key=lambda mig: mig.version)
    if [m.version for m in found] != list(range(1, len(found) + 1)):
        raise RuntimeError("migration versions must be contiguous starting at 1")
    return found


def latest_version() -> int:
    """Highest bundled migration version."""
    return all_migrations()[-1].version


def pending(current: int) -> list[Migration]:
    """Migrations with a version greater than ``current``."""
    return [m for m in all_migrations() if m.version > current]
