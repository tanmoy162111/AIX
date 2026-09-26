"""Bundle export and `aix artifact` commands (M7.4, PLAYBOOK §21.3)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import anyio
import pytest
from typer.testing import CliRunner

from aix.artifacts.bundle import BundleError, export_bundle, verify_bundle
from aix.cli.main import app
from run_env import finished_run

pytestmark = pytest.mark.anyio
runner = CliRunner()


def rewrite(src: Path, dst: Path, change: dict[str, bytes | None]) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for name in zin.namelist():
            data = change.get(name, zin.read(name))
            if data is not None:
                zout.writestr(name, data)
        for name, data in change.items():
            if name not in zin.namelist() and data is not None:
                zout.writestr(name, data)


async def test_export_verify_and_determinism(tmp_path: Path) -> None:
    repo, run_id, store = await finished_run(tmp_path)
    try:
        path = await export_bundle(store, repo, run_id)
        assert path.is_file()  # already written automatically at run end (artifacts.bundle)
        first = path.read_bytes()
        assert (await export_bundle(store, repo, run_id)).read_bytes() == first
        res = verify_bundle(path)
        assert res.ok and res.problems == [] and res.files >= 8
        names = zipfile.ZipFile(path).namelist()
        assert names == sorted(names) and "manifest.json" in names and "report.md" in names
    finally:
        await store.close()


async def test_verify_detects_tampering(tmp_path: Path) -> None:
    repo, run_id, store = await finished_run(tmp_path)
    try:
        good = await export_bundle(store, repo, run_id)
    finally:
        await store.close()
    bad = tmp_path / "bad.zip"
    cases = {
        "modified file": ({"report.md": b"forged"}, "hash mismatch: report.md"),
        "missing file": ({"plan.json": None}, "missing file: plan.json"),
        "extra file": ({"evil.sh": b"x"}, "file not in manifest: evil.sh"),
        "manifest edited": (
            {"manifest.json": b'{"artifacts": []}'},
            "does not match manifest.sha256",
        ),
        "unsafe name": ({"../escape.txt": b"x"}, "unsafe file name"),
    }
    for label, (change, expect) in cases.items():
        rewrite(good, bad, change)
        res = verify_bundle(bad)
        assert not res.ok and any(expect in p for p in res.problems), (label, res.problems)


async def test_unreadable_and_manifestless_bundles(tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_text("not a zip")
    assert not verify_bundle(junk).ok and not verify_bundle(tmp_path / "nope.zip").ok
    empty = tmp_path / "empty.zip"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("a.txt", "x")
    assert "manifest.json is missing" in verify_bundle(empty).problems


async def test_export_refuses_a_run_without_manifest(tmp_path: Path) -> None:
    repo, _, store = await finished_run(tmp_path)
    try:
        with pytest.raises(BundleError):
            await export_bundle(store, repo, "run_01ARZ3NDEKTSV4RRFFQ69G5FAV")
    finally:
        await store.close()


async def _prepared(tmp_path: Path) -> tuple[Path, str]:
    repo, run_id, store = await finished_run(tmp_path)
    await store.close()
    return repo, run_id


def test_cli_list_show_export_verify(tmp_path: Path) -> None:
    repo, run_id = anyio.run(_prepared, tmp_path)
    p = ["--project", str(repo)]
    res = runner.invoke(app, ["artifact", "list", run_id, "--json", *p])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.output)
    assert {r["name"] for r in rows} >= {"plan.json", "manifest.json", "report.md"}
    plan = next(r for r in rows if r["name"] == "plan.json")
    shown = runner.invoke(app, ["artifact", "show", plan["id"], *p])
    assert shown.exit_code == 0 and '"intent"' in shown.output
    exported = runner.invoke(app, ["artifact", "export", run_id, "--bundle", "--json", *p])
    assert exported.exit_code == 0, exported.output
    bundle = json.loads(exported.output)["bundle"]
    ok = runner.invoke(app, ["artifact", "verify", bundle])
    assert ok.exit_code == 0 and ok.output.startswith("OK:")
    rewrite(Path(bundle), tmp_path / "bad.zip", {"plan.json": b"{}"})
    bad = runner.invoke(app, ["artifact", "verify", str(tmp_path / "bad.zip")])
    assert bad.exit_code == 1 and "hash mismatch: plan.json" in bad.output
    assert runner.invoke(app, ["artifact", "show", "art_nope", *p]).exit_code == 2
    assert runner.invoke(app, ["artifact", "export", run_id, *p]).exit_code == 2
