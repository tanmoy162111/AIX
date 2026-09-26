from __future__ import annotations

from pathlib import Path

import pytest

import repos
from aix.core.workspace.manager import WorkspaceManager
from aix.domain.errors import ToolFailure
from aix.domain.ids import IdPrefix, new_id

pytestmark = pytest.mark.anyio


def rid() -> str:
    return new_id(IdPrefix.RUN)


def aid() -> str:
    return new_id(IdPrefix.ATTEMPT)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return repos.materialize_sample_py(tmp_path / "proj")


def head(repo: Path, ref: str = "HEAD") -> str:
    return repos.git(repo, "rev-parse", ref).stdout.strip()


# ---- preflight ---------------------------------------------------------------


async def test_preflight_passes_on_clean_repo(repo: Path) -> None:
    await WorkspaceManager(repo).preflight()


async def test_preflight_rejects_non_git_directory(tmp_path: Path) -> None:
    with pytest.raises(ToolFailure, match="not a git repository"):
        await WorkspaceManager(tmp_path).preflight()


async def test_preflight_rejects_dirty_tracked_files_but_not_untracked(repo: Path) -> None:
    (repo / "untracked.txt").write_text("x")
    await WorkspaceManager(repo).preflight()
    (repo / "README.md").write_text("changed\n")
    with pytest.raises(ToolFailure, match="uncommitted"):
        await WorkspaceManager(repo).preflight()


async def test_allow_dirty_snapshots_the_dirty_state_without_touching_the_user_tree(
    repo: Path,
) -> None:
    (repo / "README.md").write_text("dirty change\n")
    mgr = WorkspaceManager(repo)
    run_id = rid()
    branch = await mgr.create_run_branch(run_id, allow_dirty=True)
    shown = repos.git(repo, "show", f"{branch.name}:README.md").stdout
    assert shown == "dirty change\n"
    assert (repo / "README.md").read_text() == "dirty change\n"  # user's file untouched
    assert branch.base_commit != head(repo)


# ---- run branch and attempt workspaces -----------------------------------------------


async def test_run_branch_starts_at_head_and_user_branch_is_untouched(repo: Path) -> None:
    before = head(repo, "main")
    mgr = WorkspaceManager(repo)
    run_id = rid()
    rb = await mgr.create_run_branch(run_id)
    assert rb.name == f"aix/run/{run_id}"
    assert rb.base_commit == before == head(repo, rb.name)
    assert repos.git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "main"
    assert head(repo, "main") == before


async def test_attempt_workspace_is_a_worktree_on_its_own_branch(repo: Path) -> None:
    mgr = WorkspaceManager(repo)
    run_id, att = rid(), aid()
    rb = await mgr.create_run_branch(run_id)
    ws = await mgr.create_attempt_workspace(run_id, att)
    assert ws.path == repo.resolve() / ".aix" / "worktrees" / att
    assert ws.branch == f"aix/att/{att}"
    assert ws.base_commit == rb.base_commit
    assert (ws.path / "app" / "handler.py").exists()
    assert repos.git(ws.path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == ws.branch


async def test_attempt_workspaces_are_isolated(repo: Path) -> None:
    mgr = WorkspaceManager(repo)
    run_id = rid()
    await mgr.create_run_branch(run_id)
    a = await mgr.create_attempt_workspace(run_id, aid())
    b = await mgr.create_attempt_workspace(run_id, aid())
    (a.path / "only_in_a.txt").write_text("a")
    assert not (b.path / "only_in_a.txt").exists()
    assert not (repo / "only_in_a.txt").exists()


async def test_missing_run_branch_is_an_error(repo: Path) -> None:
    with pytest.raises(ToolFailure):
        await WorkspaceManager(repo).create_attempt_workspace(rid(), aid())


# ---- diff capture ------------------------------------------------------------------


async def _ws(repo: Path):  # type: ignore[no-untyped-def]
    mgr = WorkspaceManager(repo)
    run_id = rid()
    await mgr.create_run_branch(run_id)
    return mgr, await mgr.create_attempt_workspace(run_id, aid())


async def test_empty_diff(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    cap = await mgr.capture_diff(ws)
    assert cap.summary.files_changed == 0 and cap.patch == ""
    assert cap.summary.patch_sha256 is None


async def test_diff_counts_adds_modifies_and_deletes(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    (ws.path / "new.txt").write_text("a\nb\nc\n")
    with (ws.path / "README.md").open("a") as fh:
        fh.write("extra line\n")
    (ws.path / "app" / "users.py").unlink()
    cap = await mgr.capture_diff(ws)
    s = cap.summary
    assert s.paths == ["README.md", "app/users.py", "new.txt"]
    assert s.files_changed == 3
    assert s.lines_added == 3 + 1
    assert s.lines_removed > 10  # the deleted module
    assert s.patch_sha256 is not None and len(s.patch_sha256) == 64


async def test_captured_patch_reproduces_the_change_on_a_fresh_checkout(
    repo: Path, tmp_path: Path
) -> None:
    mgr, ws = await _ws(repo)
    repos.apply_patch(ws.path, "hello.diff")
    cap = await mgr.capture_diff(ws)
    fresh = repos.materialize_sample_py(tmp_path / "fresh")
    patch_file = tmp_path / "p.diff"
    patch_file.write_text(cap.patch)
    repos.git(fresh, "apply", str(patch_file))
    assert repos.run_pytest(fresh).returncode == 0
    assert "tests/test_hello.py" in cap.summary.paths


async def test_binary_files_are_counted_as_changed_without_lines(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    (ws.path / "blob.bin").write_bytes(bytes(range(256)) * 4)
    cap = await mgr.capture_diff(ws)
    assert cap.summary.paths == ["blob.bin"] and cap.summary.lines_added == 0
    assert "GIT binary patch" in cap.patch


async def test_unusual_file_names_survive(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    (ws.path / "sp ace é.txt").write_text("x\n")
    cap = await mgr.capture_diff(ws)
    assert cap.summary.paths == ["sp ace é.txt"]


async def test_diff_is_relative_to_base_even_after_commit(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    (ws.path / "f.txt").write_text("1\n")
    sha = await mgr.commit_attempt(ws, "aix: test")
    assert sha is not None
    cap = await mgr.capture_diff(ws)
    assert cap.summary.paths == ["f.txt"]


# ---- commit and cleanup ---------------------------------------------------------------


async def test_commit_attempt_returns_none_when_nothing_changed(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    assert await mgr.commit_attempt(ws, "aix: nothing") is None


async def test_commit_attempt_uses_aix_identity_and_moves_the_branch(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    (ws.path / "f.txt").write_text("1\n")
    sha = await mgr.commit_attempt(ws, "aix: add f")
    assert sha == head(ws.path)
    assert repos.git(ws.path, "log", "-1", "--format=%an|%s").stdout.strip() == "aix|aix: add f"


async def test_remove_workspace_deletes_dir_keeps_branch_and_is_idempotent(repo: Path) -> None:
    mgr, ws = await _ws(repo)
    (ws.path / "f.txt").write_text("dirty")  # even dirty worktrees are removed
    await mgr.remove_workspace(ws)
    assert not ws.path.exists()
    assert repos.git(repo, "rev-parse", "--verify", ws.branch).returncode == 0
    assert str(ws.path) not in repos.git(repo, "worktree", "list").stdout
    await mgr.remove_workspace(ws)


# ---- merge into the run branch -----------------------------------------------------------------


async def test_merge_attempt_creates_a_no_ff_merge_commit_without_touching_the_user_tree(
    repo: Path,
) -> None:
    from aix.core.workspace.manager import run_branch_name

    mgr = WorkspaceManager(repo)
    run_id = rid()
    rb = await mgr.create_run_branch(run_id)
    ws = await mgr.create_attempt_workspace(run_id, aid())
    repos.apply_patch(ws.path, "hello.diff")
    assert await mgr.commit_attempt(ws, "aix: hello") is not None
    merge_sha = await mgr.merge_attempt(run_id, ws, "aix: merge hello")
    assert head(repo, run_branch_name(run_id)) == merge_sha
    parents = repos.git(repo, "rev-list", "--parents", "-n", "1", merge_sha).stdout.split()[1:]
    assert len(parents) == 2 and parents[0] == rb.base_commit
    assert "/hello" in repos.git(repo, "show", f"{merge_sha}:app/handler.py").stdout
    assert head(repo, "main") == rb.base_commit
    assert repos.git(repo, "status", "--porcelain", "--untracked-files=no").stdout == ""


async def test_second_merge_builds_on_the_first_and_conflicts_are_detected(repo: Path) -> None:
    from aix.domain.errors import MergeConflict

    mgr = WorkspaceManager(repo)
    run_id = rid()
    await mgr.create_run_branch(run_id)
    a = await mgr.create_attempt_workspace(run_id, aid())
    b = await mgr.create_attempt_workspace(run_id, aid())
    (a.path / "README.md").write_text("from a\n")
    (b.path / "README.md").write_text("from b\n")
    (b.path / "other.txt").write_text("b only\n")
    await mgr.commit_attempt(a, "aix: a")
    await mgr.commit_attempt(b, "aix: b")
    await mgr.merge_attempt(run_id, a, "merge a")
    with pytest.raises(MergeConflict) as ei:
        await mgr.merge_attempt(run_id, b, "merge b")
    assert ei.value.details["files"] == ["README.md"]


async def test_merge_of_disjoint_attempts_keeps_both_changes(repo: Path) -> None:
    from aix.core.workspace.manager import run_branch_name

    mgr = WorkspaceManager(repo)
    run_id = rid()
    await mgr.create_run_branch(run_id)
    a = await mgr.create_attempt_workspace(run_id, aid())
    b = await mgr.create_attempt_workspace(run_id, aid())
    (a.path / "a.txt").write_text("a\n")
    (b.path / "b.txt").write_text("b\n")
    await mgr.commit_attempt(a, "aix: a")
    await mgr.commit_attempt(b, "aix: b")
    await mgr.merge_attempt(run_id, a, "merge a")
    sha = await mgr.merge_attempt(run_id, b, "merge b")
    files = repos.git(repo, "ls-tree", "-r", "--name-only", sha).stdout.split()
    assert "a.txt" in files and "b.txt" in files
    assert head(repo, run_branch_name(run_id)) == sha


async def test_merging_an_attempt_without_commits_is_an_error(repo: Path) -> None:
    mgr = WorkspaceManager(repo)
    run_id = rid()
    await mgr.create_run_branch(run_id)
    ws = await mgr.create_attempt_workspace(run_id, aid())
    with pytest.raises(ToolFailure, match="no commits"):
        await mgr.merge_attempt(run_id, ws, "nothing")
