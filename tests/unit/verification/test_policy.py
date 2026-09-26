from __future__ import annotations

from pathlib import Path

import pytest

from aix.domain.enums import CheckKind as K
from aix.verification.policy import run_policy_check

pytestmark = pytest.mark.anyio


def write(root: Path, rel: str, data: bytes | str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data if isinstance(data, bytes) else data.encode())


async def check(root: Path, paths: list[str], scope: list[str] | None = None, **kw: object):  # type: ignore[no-untyped-def]
    return await run_policy_check(root, paths, scope if scope is not None else ["**"], **kw)  # type: ignore[arg-type]


async def test_clean_change_passes(tmp_path: Path) -> None:
    write(tmp_path, "src/a.py", "x = 1\n")
    r = await check(tmp_path, ["src/a.py"], ["src/**"])
    assert r.status == "passed" and r.kind is K.POLICY and r.required
    assert r.metrics == {"policy_violations": 0.0}


async def test_scope_violation_and_read_only(tmp_path: Path) -> None:
    write(tmp_path, "docs/x.md", "hi")
    r = await check(tmp_path, ["docs/x.md"], ["src/**"])
    assert r.status == "failed" and "outside the task's file scope" in r.summary
    assert "docs/x.md" in r.summary and r.metrics == {"policy_violations": 1.0}
    ro = await check(tmp_path, ["docs/x.md"], [])
    assert ro.status == "failed" and "read-only" in ro.summary


async def test_control_dirs_are_always_forbidden(tmp_path: Path) -> None:
    r = await check(tmp_path, [".aix/config.yaml", ".git/hooks/pre-commit"])
    assert r.status == "failed" and r.metrics["policy_violations"] == 2.0


@pytest.mark.parametrize(
    "path",
    [".env", ".env.production", "cfg/.env", "deploy/server.pem", "id_rsa", "keys/a.p12",
     ".aws/credentials", "credentials.json"],
)  # fmt: skip
async def test_forbidden_file_types(tmp_path: Path, path: str) -> None:
    write(tmp_path, path, "x")
    r = await check(tmp_path, [path])
    assert r.status == "failed" and "forbidden file type" in r.summary


@pytest.mark.parametrize("path", [".env.example", ".env.sample", "env.py", "keys.md"])
async def test_lookalikes_are_allowed(tmp_path: Path, path: str) -> None:
    write(tmp_path, path, "x")
    assert (await check(tmp_path, [path])).status == "passed"


async def test_only_large_binary_blobs_fail(
    tmp_path: Path,
) -> None:
    write(tmp_path, "big.bin", b"\x00" * 5000)
    write(tmp_path, "small.bin", b"\x00" * 10)
    write(tmp_path, "big.txt", "a" * 5000)
    r = await check(tmp_path, ["big.bin", "small.bin", "big.txt"], max_binary_bytes=1000)
    assert r.status == "failed" and "big.bin" in r.summary
    assert "small.bin" not in r.summary and "big.txt" not in r.summary
    assert r.metrics["policy_violations"] == 1.0


async def test_deleted_files_are_not_size_checked(tmp_path: Path) -> None:
    assert (await check(tmp_path, ["gone.bin"])).status == "passed"
