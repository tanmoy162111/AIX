"""Git-worktree workspaces (PLAYBOOK §14.3, §10.4).

* The run branch ``aix/run/<run_id>`` is the integration branch, created from HEAD (or from a
  snapshot of dirty tracked files with ``allow_dirty``). The user's branch is never modified.
* Each attempt runs in ``.aix/worktrees/<attempt_id>`` on branch ``aix/att/<attempt_id>``.
* The control plane, not the agent, computes what changed (``capture_diff``).
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path

import anyio

from aix.domain.base import Sha256
from aix.domain.errors import MergeConflict, ToolFailure
from aix.domain.execution import DiffSummary
from aix.tools.git import git


@dataclass(frozen=True)
class RunBranch:
    name: str
    base_commit: str


@dataclass(frozen=True)
class Workspace:
    path: Path
    branch: str
    base_commit: str


@dataclass(frozen=True)
class DiffCapture:
    summary: DiffSummary
    patch: str


def run_branch_name(run_id: str) -> str:
    return f"aix/run/{run_id}"


def attempt_branch_name(attempt_id: str) -> str:
    return f"aix/att/{attempt_id}"


class WorkspaceManager:
    """Creates, inspects and removes the git state aix needs for a project."""

    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()
        self._merge_lock = anyio.Lock()

    @property
    def worktrees_dir(self) -> Path:
        return self.root / ".aix" / "worktrees"

    async def _is_dirty(self) -> bool:
        res = await git(self.root, "status", "--porcelain", "--untracked-files=no")
        return bool(res.stdout.strip())

    async def preflight(self, *, allow_dirty: bool = False) -> None:
        """Require a git repository with a clean tree of tracked files.

        Raises:
            ToolFailure: not a git repository, or uncommitted changes without ``allow_dirty``.
        """
        res = await git(self.root, "rev-parse", "--is-inside-work-tree", check=False)
        if res.code != 0 or res.stdout.strip() != "true":
            raise ToolFailure(f"{self.root} is not a git repository (run `git init`)")
        if not allow_dirty and await self._is_dirty():
            raise ToolFailure(
                "the working tree has uncommitted changes to tracked files; commit or stash them, "
                "or pass --allow-dirty"
            )

    async def create_run_branch(self, run_id: str, *, allow_dirty: bool = False) -> RunBranch:
        """Create ``aix/run/<run_id>`` from HEAD, or from a snapshot of the dirty tracked state.

        The user's working tree and checked-out branch are left exactly as they were.
        """
        await self.preflight(allow_dirty=allow_dirty)
        base = (await git(self.root, "rev-parse", "HEAD")).stdout.strip()
        if allow_dirty and await self._is_dirty():
            snap = (await git(self.root, "stash", "create", f"aix snapshot for {run_id}")).stdout
            base = snap.strip() or base
        name = run_branch_name(run_id)
        await git(self.root, "branch", name, base)
        return RunBranch(name=name, base_commit=base)

    async def create_attempt_workspace(self, run_id: str, attempt_id: str) -> Workspace:
        """Add a worktree for ``attempt_id`` at the run branch's current HEAD."""
        run_branch = run_branch_name(run_id)
        base = (await git(self.root, "rev-parse", "--verify", run_branch)).stdout.strip()
        path = self.worktrees_dir / attempt_id
        branch = attempt_branch_name(attempt_id)
        await anyio.Path(self.worktrees_dir).mkdir(parents=True, exist_ok=True)
        await git(self.root, "worktree", "add", "-q", "-b", branch, str(path), run_branch)
        return Workspace(path=path, branch=branch, base_commit=base)

    async def capture_diff(self, ws: Workspace) -> DiffCapture:
        """Compute the attempt's change relative to its base commit (including commits made since).

        Stages everything (``git add -A``) and diffs the index against ``base_commit``; renames are
        reported as delete + add so paths stay simple.
        """
        await git(ws.path, "add", "-A")
        numstat = (
            await git(
                ws.path, "diff", "--cached", "--numstat", "-z", "--no-renames", ws.base_commit
            )
        ).stdout
        patch = (
            await git(ws.path, "diff", "--cached", "--binary", "--no-renames", ws.base_commit)
        ).stdout
        paths: list[str] = []
        added = removed = 0
        for entry in filter(None, numstat.split("\0")):
            a, r, path = entry.split("\t", 2)
            paths.append(path)
            added += int(a) if a.isdigit() else 0
            removed += int(r) if r.isdigit() else 0
        digest: Sha256 | None = hashlib.sha256(patch.encode("utf-8")).hexdigest() if patch else None
        summary = DiffSummary(
            files_changed=len(paths),
            lines_added=added,
            lines_removed=removed,
            paths=sorted(paths),
            patch_sha256=digest,
        )
        return DiffCapture(summary=summary, patch=patch)

    async def commit_attempt(self, ws: Workspace, message: str) -> str | None:
        """Commit worktree changes as ``aix``; returns the sha, or ``None`` if nothing changed."""
        await git(ws.path, "add", "-A")
        if (await git(ws.path, "diff", "--cached", "--quiet", check=False)).code == 0:
            return None
        await git(
            ws.path, "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", message
        )
        return (await git(ws.path, "rev-parse", "HEAD")).stdout.strip()

    async def merge_attempt(self, run_id: str, ws: Workspace, message: str) -> str:
        """Merge the attempt branch into the run branch with a ``--no-ff`` style merge commit.

        Uses ``git merge-tree --write-tree`` plus ``commit-tree``/``update-ref`` so nothing is
        checked out and the user's working tree is untouched. Merges are serialized by a lock.

        Returns the merge commit sha.

        Raises:
            ToolFailure: the attempt branch has no commits beyond the run branch.
            MergeConflict: the branches conflict; ``details["files"]`` lists the paths.
        """
        run_ref = run_branch_name(run_id)
        async with self._merge_lock:
            run_head = (await git(self.root, "rev-parse", "--verify", run_ref)).stdout.strip()
            att_head = (await git(self.root, "rev-parse", "--verify", ws.branch)).stdout.strip()
            if (
                att_head == run_head
                or (
                    await git(
                        self.root, "merge-base", "--is-ancestor", att_head, run_head, check=False
                    )
                ).code
                == 0
            ):
                raise ToolFailure(f"attempt branch {ws.branch} has no commits to merge")
            tree_res = await git(
                self.root,
                "merge-tree",
                "--write-tree",
                "--no-messages",
                "--name-only",
                run_ref,
                ws.branch,
                check=False,
            )
            lines = tree_res.stdout.splitlines()
            if tree_res.code == 1:
                files = sorted(line for line in lines[1:] if line.strip())
                raise MergeConflict(
                    f"merging {ws.branch} into {run_ref} conflicts in {len(files)} file(s)",
                    details={"files": list(files), "branch": ws.branch},
                )
            if tree_res.code != 0 or not lines:
                raise ToolFailure(f"git merge-tree failed: {tree_res.stderr.strip()}")
            commit = (
                await git(
                    self.root,
                    "commit-tree",
                    lines[0],
                    "-p",
                    run_head,
                    "-p",
                    att_head,
                    "-m",
                    message,
                )
            ).stdout.strip()
            await git(self.root, "update-ref", f"refs/heads/{run_ref}", commit, run_head)
            return commit

    async def remove_workspace(self, ws: Workspace) -> None:
        """Remove the worktree directory (even if dirty). The attempt branch is kept. Idempotent."""
        await git(self.root, "worktree", "remove", "--force", str(ws.path), check=False)
        if await anyio.Path(ws.path).exists():
            await anyio.to_thread.run_sync(shutil.rmtree, ws.path, True)
        await git(self.root, "worktree", "prune", check=False)
