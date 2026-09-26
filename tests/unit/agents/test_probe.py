from __future__ import annotations

from pathlib import Path

import pytest

from aix.agents.manifest import AdapterManifest, ProbeSpec
from aix.agents.probe import probe_binary
from binaries import env_with_path, make_fake_binary

pytestmark = pytest.mark.anyio


def manifest(**kw: object) -> AdapterManifest:
    data: dict[str, object] = {
        "id": "tool",
        "name": "Tool",
        "kind": "cli",
        "binary": "faketool",
        "cost_class": "low",
        "probe": ProbeSpec(version_args=["--version"], help_args=["--help"]),
        "required_flags": ["--json", "--model"],
    }
    data.update(kw)
    return AdapterManifest.model_validate(data)


HELP_OK = """
import sys
if "--version" in sys.argv: print("faketool 1.2.3-beta")
elif "--help" in sys.argv: print("usage: faketool --json --model X")
"""


async def test_missing_binary_is_unavailable(tmp_path: Path) -> None:
    r = await probe_binary(manifest(binary="nope-xyz"), env=env_with_path(tmp_path))
    assert r.health == "unavailable" and r.version is None
    assert "not found" in (r.reason or "")


async def test_ready_with_version_and_all_flags(tmp_path: Path) -> None:
    make_fake_binary(tmp_path, "faketool", HELP_OK)
    r = await probe_binary(manifest(), env=env_with_path(tmp_path))
    assert r.health == "ready" and r.reason is None
    assert r.version == "1.2.3-beta"
    assert r.missing_flags == []


async def test_missing_flags_degrade_with_reason(tmp_path: Path) -> None:
    make_fake_binary(
        tmp_path,
        "faketool",
        """
        import sys
        if "--version" in sys.argv: print("2.0.0")
        else: print("usage: faketool --json")
        """,
    )
    r = await probe_binary(manifest(), env=env_with_path(tmp_path))
    assert r.health == "degraded" and r.missing_flags == ["--model"]
    assert "--model" in (r.reason or "")


async def test_no_required_flags_skips_help(tmp_path: Path) -> None:
    make_fake_binary(tmp_path, "faketool", 'print("v9.9.9")\n')
    r = await probe_binary(manifest(required_flags=[]), env=env_with_path(tmp_path))
    assert r.health == "ready" and r.version == "9.9.9"


async def test_hanging_binary_times_out_as_unavailable(tmp_path: Path) -> None:
    make_fake_binary(tmp_path, "faketool", "import time; time.sleep(60)\n")
    r = await probe_binary(manifest(), env=env_with_path(tmp_path), timeout_s=1)
    assert r.health == "unavailable" and "timed out" in (r.reason or "")


async def test_failing_version_command_is_unavailable(tmp_path: Path) -> None:
    make_fake_binary(tmp_path, "faketool", "import sys; sys.stderr.write('bad'); sys.exit(4)\n")
    r = await probe_binary(manifest(), env=env_with_path(tmp_path))
    assert r.health == "unavailable" and "exit" in (r.reason or "")


async def test_manifest_without_binary_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        await probe_binary(manifest(binary=None), env=env_with_path(tmp_path))
