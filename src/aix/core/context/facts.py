"""Project context store (PLAYBOOK §15.1): cached, HEAD-keyed project facts."""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Final

import anyio
from pydantic import Field, ValidationError

from aix.domain.base import DomainModel
from aix.domain.errors import ToolFailure
from aix.domain.tasks import RepoFacts
from aix.security.redact import redact_secrets
from aix.tools.git import git
from aix.verification.detect import inspect_repo

CONVENTION_FILES: Final = ("CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", "README.md")
CONVENTION_EXCERPT_CHARS: Final = 4000
_CACHE_REL: Final = Path(".aix") / "cache" / "project_facts.json"
_TRUNCATED: Final = "\n[... truncated]"


class ProjectFacts(DomainModel):
    """What every agent should know about the project; secret-redacted, size-capped."""

    head: str | None = None
    """Commit the facts were read at; ``None`` outside git (never cached then)."""
    repo: RepoFacts = Field(default_factory=RepoFacts)
    conventions: dict[str, str] = Field(default_factory=dict[str, str])
    """Convention file name -> redacted excerpt (first ``CONVENTION_EXCERPT_CHARS`` chars)."""


async def _head(root: Path) -> str | None:
    try:
        res = await git(root, "rev-parse", "--verify", "HEAD", check=False)
    except ToolFailure:  # git missing: behave like a non-git directory
        return None
    out = res.stdout.strip()
    return out if res.code == 0 and out else None


def _excerpt(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    text = redact_secrets(text)
    return (
        text[:CONVENTION_EXCERPT_CHARS] + _TRUNCATED
        if len(text) > CONVENTION_EXCERPT_CHARS
        else text
    )


def _read(root: Path, head: str | None) -> ProjectFacts:
    conventions = {n: _excerpt(root / n) for n in CONVENTION_FILES if (root / n).is_file()}
    return ProjectFacts(head=head, repo=inspect_repo(root), conventions=conventions)


def _cached(root: Path, head: str) -> ProjectFacts | None:
    try:
        facts = ProjectFacts.model_validate_json((root / _CACHE_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError):
        return None
    return facts if facts.head == head else None


async def load_project_facts(root: Path) -> ProjectFacts:
    """Return project facts for ``root``, reading the cache when HEAD is unchanged.

    The cache lives at ``.aix/cache/project_facts.json`` and is keyed on the HEAD commit; a
    missing, corrupt or stale cache is silently rebuilt. Outside a git repo (or with no commit)
    facts are recomputed every call and nothing is written. Never raises for missing files.
    """
    head = await _head(root)
    if head is not None:
        hit = await anyio.to_thread.run_sync(_cached, root, head)
        if hit is not None:
            return hit
    facts = await anyio.to_thread.run_sync(_read, root, head)
    if head is not None:

        def _write() -> None:
            with contextlib.suppress(OSError):
                (root / _CACHE_REL).parent.mkdir(parents=True, exist_ok=True)
                (root / _CACHE_REL).write_text(facts.model_dump_json(indent=2), encoding="utf-8")

        await anyio.to_thread.run_sync(_write)
    return facts
