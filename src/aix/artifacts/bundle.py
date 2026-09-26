"""Evidence bundles: export a run's artifacts to a zip and verify one (PLAYBOOK §21.3)."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

import anyio

from aix.artifacts.report import artifact_names
from aix.artifacts.store import ObjectStore
from aix.domain.enums import ArtifactType
from aix.store.db import EventStore

MANIFEST: Final = "manifest.json"
MANIFEST_HASH: Final = "manifest.sha256"
_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


class BundleError(Exception):
    """The run has no manifest, or a blob it names is missing or corrupt."""


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    files: int
    problems: list[str] = field(default_factory=list[str])


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(name: str) -> bool:
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and ".." not in p.parts and "\\" not in name


def bundle_path(root: Path, run_id: str) -> Path:
    return root / ".aix" / "artifacts" / "bundles" / f"{run_id}.zip"


async def export_bundle(store: EventStore, root: Path, run_id: str) -> Path:
    """Write ``.aix/artifacts/bundles/<run>.zip`` and return its path.

    Contract: the zip holds ``manifest.json``, ``manifest.sha256`` (hash of the manifest bytes)
    and every artifact under its manifest name. Entries are sorted with a fixed timestamp, so
    exporting the same run twice yields identical bytes. Every blob is re-hashed while packing.

    Raises:
        BundleError: no manifest was written for the run, or a blob is missing or corrupt.
    """
    manifests = [a for a in await store.get_artifacts(run_id) if a.type is ArtifactType.MANIFEST]
    if not manifests:
        raise BundleError(f"run {run_id} has no manifest (it has not finished)")
    objects = ObjectStore(root / ".aix" / "artifacts" / "objects")
    manifest_bytes = await objects.get(manifests[-1].sha256)
    doc = json.loads(manifest_bytes)
    files: dict[str, bytes] = {
        MANIFEST: manifest_bytes,
        MANIFEST_HASH: (_sha(manifest_bytes) + "\n").encode(),
    }
    for entry in doc["artifacts"]:
        try:
            data = await objects.get(entry["sha256"])
        except FileNotFoundError as exc:
            raise BundleError(f"blob for {entry['name']} is missing") from exc
        if _sha(data) != entry["sha256"]:
            raise BundleError(f"blob for {entry['name']} is corrupt")
        files[entry["name"]] = data
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    target = anyio.Path(bundle_path(root, run_id))
    await target.parent.mkdir(parents=True, exist_ok=True)
    await target.write_bytes(buf.getvalue())
    return Path(target)


def verify_bundle(path: Path) -> VerifyResult:
    """Re-hash every file of a bundle and check it against ``manifest.json``.

    Reports (never raises for) a missing manifest, a manifest whose hash differs from
    ``manifest.sha256``, unsafe names, missing, extra or modified files and size mismatches.
    A forger who edits a file *and* rewrites both manifest files is not detectable without a
    signature; compare ``manifest.sha256`` with the hash recorded in the event store to close that.
    """
    problems: list[str] = []
    try:
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        return VerifyResult(False, 0, [f"cannot read bundle: {exc}"])
    with zf:
        names = zf.namelist()
        for n in names:
            if not _safe(n):
                problems.append(f"unsafe file name: {n!r}")
        if MANIFEST not in names:
            return VerifyResult(False, len(names), [*problems, "manifest.json is missing"])
        manifest_bytes = zf.read(MANIFEST)
        if MANIFEST_HASH not in names:
            problems.append("manifest.sha256 is missing")
        elif zf.read(MANIFEST_HASH).decode().strip() != _sha(manifest_bytes):
            problems.append("manifest.json does not match manifest.sha256")
        try:
            listed = json.loads(manifest_bytes)["artifacts"]
        except (ValueError, KeyError, TypeError):
            return VerifyResult(False, len(names), [*problems, "manifest.json is malformed"])
        expected: set[str] = set()
        for entry in listed:
            name = str(entry.get("name", ""))
            expected.add(name)
            if name not in names:
                problems.append(f"missing file: {name}")
                continue
            data = zf.read(name)
            if _sha(data) != entry.get("sha256"):
                problems.append(f"hash mismatch: {name}")
            elif len(data) != entry.get("size"):
                problems.append(f"size mismatch: {name}")
        for n in names:
            if n not in expected and n not in (MANIFEST, MANIFEST_HASH):
                problems.append(f"file not in manifest: {n}")
        return VerifyResult(not problems, len(names), problems)


async def manifest_names(store: EventStore, root: Path, run_id: str) -> list[str]:
    """Re-export of :func:`aix.artifacts.report.artifact_names` for CLI callers."""
    return await artifact_names(store, root, run_id)
