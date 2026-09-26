"""The current change of a working tree, as a patch and a path list (for ``aix verify|review``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import anyio

from aix.tools.git import git

MAX_UNTRACKED_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class TreeChange:
    """Everything that differs from the base: tracked edits plus untracked files."""

    patch: str = ""
    paths: list[str] = field(default_factory=list[str])
    is_git: bool = False

    @property
    def empty(self) -> bool:
        return not self.patch.strip() and not self.paths


def _own(rel: str) -> bool:
    """aiX's own runtime directory is never part of the user's change."""
    return rel == ".aix" or rel.startswith(".aix/")


def _added_file_diff(rel: str, text: str) -> str:
    lines = text.splitlines()
    body = "".join(f"+{line}\n" for line in lines)
    head = f"diff --git a/{rel} b/{rel}\n--- /dev/null\n+++ b/{rel}\n"
    return f"{head}@@ -0,0 +1,{len(lines)} @@\n{body}"


async def working_tree_change(root: Path, base: str | None = None) -> TreeChange:
    """Change of ``root`` against ``base`` (default ``HEAD``); a non-git directory has none.

    Untracked, non-ignored text files up to 1 MiB are included as added files so secrets in new
    files are seen. Paths under ``.aix/`` are ignored; tracked edits there are dropped from the
    path list but remain in the tracked patch text only if committed (they never are by aiX).
    """
    probe = await git(root, "rev-parse", "--is-inside-work-tree", check=False)
    if probe.code != 0:
        return TreeChange()
    ref = base or "HEAD"
    head = await git(root, "rev-parse", "--verify", ref, check=False)
    if head.code != 0:
        return TreeChange(is_git=True)
    tracked = (await git(root, "diff", "--binary", "--no-renames", ref)).stdout
    names = (await git(root, "diff", "--name-only", "-z", "--no-renames", ref)).stdout
    paths = [p for p in names.split("\0") if p and not _own(p)]
    extra: list[str] = []
    untracked = (await git(root, "ls-files", "-o", "--exclude-standard", "-z")).stdout
    for rel in (p for p in untracked.split("\0") if p and not _own(p)):
        paths.append(rel)
        path = anyio.Path(root / rel)
        try:
            if not await path.is_file() or (await path.stat()).st_size > MAX_UNTRACKED_BYTES:
                continue
            data = await path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        extra.append(_added_file_diff(rel, data.decode("utf-8", errors="replace")))
    return TreeChange(tracked + "".join(extra), sorted(set(paths)), True)
