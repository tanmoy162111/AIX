from __future__ import annotations

import pytest

from aix.core.toolrisk import classify_command


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["git", "push", "origin", "main"], {"git_push"}),
        (["git", "push"], {"git_push"}),
        (["git", "status"], {"read_only"}),
        (["git", "diff", "HEAD"], {"read_only"}),
        (["git", "clean", "-fdx"], {"local_delete"}),
        (["git", "reset", "--hard"], {"local_delete"}),
        (["kubectl", "apply", "-f", "x.yaml"], {"deploy"}),
        (["terraform", "apply"], {"deploy"}),
        (["helm", "upgrade", "app", "."], {"deploy"}),
        (["npm", "publish"], {"deploy"}),
        (["make", "deploy"], {"deploy"}),
        (["docker", "push", "img"], {"deploy"}),
        (["alembic", "upgrade", "head"], {"db_migration_apply"}),
        (["python", "manage.py", "migrate"], {"db_migration_apply"}),
        (["prisma", "migrate", "deploy"], {"db_migration_apply", "deploy"}),
        (["rm", "-rf", "build"], {"local_delete"}),
        (["curl", "https://x"], {"external_network"}),
        (["ssh", "host", "ls"], {"external_network"}),
        (["pytest", "-q"], {"local_write"}),
        (["python", "-m", "compileall", "-q", "src"], {"local_write"}),
        (["ruff", "check"], {"local_write"}),
        (["npm", "test"], {"local_write"}),
        (["make", "test"], {"local_write"}),
        (["/usr/bin/git", "push"], {"git_push"}),
        (["cat", "README.md"], {"read_only"}),
        (["totally-unknown-tool"], {"unknown"}),
    ],
)
def test_classification(argv: list[str], expected: set[str]) -> None:
    assert set(classify_command(argv)) == expected


def test_classes_are_deduplicated_and_sorted() -> None:
    got = classify_command(["sh", "-c", "git push && curl x"])
    assert got == sorted(set(got))
    assert "git_push" in got and "external_network" in got


def test_empty_argv_is_unknown() -> None:
    assert classify_command([]) == ["unknown"]
