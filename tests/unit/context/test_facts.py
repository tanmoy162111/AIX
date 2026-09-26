from __future__ import annotations

from pathlib import Path

import pytest

from aix.core.context.facts import CONVENTION_EXCERPT_CHARS, load_project_facts
from aix.tools.git import git

pytestmark = pytest.mark.anyio


async def _repo(root: Path) -> None:
    await git(root, "init", "-q", "-b", "main")
    await git(root, "config", "user.email", "t@example.invalid")
    await git(root, "config", "user.name", "t")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    (root / "CLAUDE.md").write_text("# Rules\nUse `make check`.\n")
    (root / "README.md").write_text("# X\n")
    await git(root, "add", "-A")
    await git(root, "commit", "-q", "-m", "init")


async def test_reads_convention_files_and_repo_facts(tmp_path: Path) -> None:
    await _repo(tmp_path)
    facts = await load_project_facts(tmp_path)
    assert facts.repo.languages == ["python"]
    assert set(facts.conventions) == {"CLAUDE.md", "README.md"}
    assert "make check" in facts.conventions["CLAUDE.md"]
    assert facts.head is not None and len(facts.head) == 40


async def test_excerpts_are_capped_and_redacted(tmp_path: Path) -> None:
    await _repo(tmp_path)
    secret = 'api_key = "abcdefghijklmnop1234567890"\n'
    (tmp_path / "AGENTS.md").write_text(secret + "x" * (CONVENTION_EXCERPT_CHARS * 2))
    facts = await load_project_facts(tmp_path)
    text = facts.conventions["AGENTS.md"]
    assert "abcdefghijklmnop1234567890" not in text and "[REDACTED]" in text
    assert len(text) <= CONVENTION_EXCERPT_CHARS + 40  # cap + truncation marker


async def test_cache_is_reused_while_head_and_files_are_unchanged(tmp_path: Path) -> None:
    await _repo(tmp_path)
    first = await load_project_facts(tmp_path)
    cache = tmp_path / ".aix" / "cache" / "project_facts.json"
    assert cache.is_file()
    cache.write_text(cache.read_text().replace("Use `make check`.", "CACHED-MARKER"))
    assert "CACHED-MARKER" in (await load_project_facts(tmp_path)).conventions["CLAUDE.md"]
    assert first.head == (await load_project_facts(tmp_path)).head


async def test_cache_refreshes_on_head_change(tmp_path: Path) -> None:
    await _repo(tmp_path)
    old = await load_project_facts(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# New rules\n")
    await git(tmp_path, "commit", "-qam", "edit")
    new = await load_project_facts(tmp_path)
    assert new.head != old.head and "New rules" in new.conventions["CLAUDE.md"]


async def test_corrupt_cache_is_ignored(tmp_path: Path) -> None:
    await _repo(tmp_path)
    await load_project_facts(tmp_path)
    (tmp_path / ".aix" / "cache" / "project_facts.json").write_text("{nope")
    assert "CLAUDE.md" in (await load_project_facts(tmp_path)).conventions


async def test_non_git_directory_works_without_cache(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# hi\n")
    facts = await load_project_facts(tmp_path)
    assert facts.head is None and "README.md" in facts.conventions
    assert not (tmp_path / ".aix" / "cache").exists()
