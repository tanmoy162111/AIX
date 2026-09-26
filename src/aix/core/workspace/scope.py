"""Scope checking for changed paths (PLAYBOOK §10.4).

Globs: ``**`` spans directories, ``*`` and ``?`` stay within one path segment, a trailing ``/``
means "everything below". Paths under ``.aix/`` or ``.git/`` at the repo root are never allowed.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

_ALWAYS_FORBIDDEN_ROOTS = (".aix", ".git")


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(pattern: str, path: str) -> bool:
    """Whether ``path`` (repo-relative, ``/`` separated) matches ``pattern``."""
    return _compile(pattern).match(path) is not None


def scope_violations(
    paths: Iterable[str], file_scope: Iterable[str], *, read_only: bool = False
) -> list[str]:
    """Changed ``paths`` that a task was not allowed to touch (sorted, unique).

    A read-only task may not change anything. Otherwise a path must match one of ``file_scope``;
    an empty scope on a write task therefore allows nothing.
    """
    scope = list(file_scope)
    bad: set[str] = set()
    for path in paths:
        root = path.split("/", 1)[0]
        if (
            read_only
            or root in _ALWAYS_FORBIDDEN_ROOTS
            or not any(glob_match(g, path) for g in scope)
        ):
            bad.add(path)
    return sorted(bad)
