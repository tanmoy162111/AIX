from __future__ import annotations

import pytest

from aix.core.workspace.scope import glob_match, scope_violations


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        ("src/**", "src/a.py", True),
        ("src/**", "src/a/b/c.py", True),
        ("src/**", "srcx/a.py", False),
        ("src/**", "other/src/a.py", False),
        ("**/*.py", "a.py", True),
        ("**/*.py", "a/b/c.py", True),
        ("**/*.py", "a/b/c.txt", False),
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),
        ("app/?.py", "app/a.py", True),
        ("app/?.py", "app/ab.py", False),
        ("src/", "src/a/b.py", True),
        ("src", "src/a.py", False),
        ("app/auth.py", "app/auth.py", True),
        ("app/auth.py", "app/authx.py", False),
        ("a.b", "aXb", False),
    ],
)
def test_glob_match(pattern: str, path: str, expected: bool) -> None:
    assert glob_match(pattern, path) is expected


def test_read_only_task_any_change_is_a_violation() -> None:
    assert scope_violations(["a.py"], [], read_only=True) == ["a.py"]
    assert scope_violations([], [], read_only=True) == []


def test_write_task_allows_only_scoped_paths() -> None:
    v = scope_violations(["app/a.py", "deploy/x.yaml", "tests/t.py"], ["app/**", "tests/**"])
    assert v == ["deploy/x.yaml"]


def test_empty_scope_on_write_task_means_no_paths_allowed() -> None:
    assert scope_violations(["a.py"], []) == ["a.py"]


def test_aix_and_git_dirs_are_always_violations() -> None:
    v = scope_violations([".aix/config.yaml", ".git/hooks/x", "a/.aix/x"], ["**"])
    assert v == [".aix/config.yaml", ".git/hooks/x"]


def test_result_is_sorted_and_unique() -> None:
    assert scope_violations(["z", "a", "z"], ["nothing/**"]) == ["a", "z"]
