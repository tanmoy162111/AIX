"""Content-addressed artifact store and writer (PLAYBOOK §21.1, §21.2)."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio

from aix.core.orchestrator.recorder import RunRecorder
from aix.domain.artifacts import Artifact, Producer, Provenance
from aix.domain.enums import ArtifactType
from aix.domain.ids import IdPrefix, new_id
from aix.security.redact import redact_secrets
from aix.store import events as ev

_SHA: Final = re.compile(r"^[0-9a-f]{64}$")
_TEXT_TYPES: Final = ("text/", "application/json", "application/xml", "application/x-ndjson")


@dataclass(frozen=True)
class Blob:
    sha256: str
    size: int
    relpath: str
    """Path of the blob relative to the store root: ``<sha[:2]>/<sha>``."""


def _check(sha256: str) -> str:
    if not _SHA.fullmatch(sha256):
        raise ValueError(f"not a sha256 hex digest: {sha256!r}")
    return sha256


class ObjectStore:
    """Immutable blobs addressed by SHA-256 under ``root`` (``.aix/artifacts/objects``)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, sha256: str) -> anyio.Path:
        _check(sha256)
        return anyio.Path(self.root / sha256[:2] / sha256)

    async def put(self, data: bytes) -> Blob:
        """Store ``data``; the same content always yields the same blob (written once).

        A blob whose bytes no longer match its name is replaced, so a re-put repairs corruption.
        """
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if not await self.verify(sha):
            await path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{sha}.{os.getpid()}.tmp")
            await tmp.write_bytes(data)
            await tmp.replace(path)
        return Blob(sha, len(data), f"{sha[:2]}/{sha}")

    async def get(self, sha256: str) -> bytes:
        """Return the blob bytes.

        Raises:
            ValueError: ``sha256`` is not a hex digest.
            FileNotFoundError: no such blob.
        """
        return await self._path(sha256).read_bytes()

    async def exists(self, sha256: str) -> bool:
        return bool(_SHA.fullmatch(sha256)) and await self._path(sha256).is_file()

    async def verify(self, sha256: str) -> bool:
        """True when the blob exists and re-hashes to its name."""
        if not await self.exists(sha256):
            return False
        return hashlib.sha256(await self._path(sha256).read_bytes()).hexdigest() == sha256


class ArtifactWriter:
    """Stores a blob and records ``artifact.created`` with provenance for one run."""

    def __init__(
        self,
        store: ObjectStore,
        rec: RunRecorder,
        *,
        aix_version: str,
        policy_hash: str | None = None,
    ) -> None:
        self._store = store
        self._rec = rec
        self._version = aix_version
        self._policy_hash = policy_hash

    async def write(
        self,
        type: ArtifactType,
        data: bytes,
        media_type: str,
        *,
        producer: Producer,
        task_id: str | None = None,
        attempt_id: str | None = None,
        tools: list[str] | None = None,
        inputs: list[str] | None = None,
        base_commit: str | None = None,
        result_commit: str | None = None,
        decisions: list[str] | None = None,
    ) -> Artifact:
        """Store ``data`` and emit ``artifact.created``; returns the recorded artifact.

        Contract: text media types are secret-redacted before hashing (§20.5); binary content is
        stored verbatim. Identical content shares one blob but each call records a new artifact.
        """
        if media_type.startswith(_TEXT_TYPES):
            data = redact_secrets(data.decode("utf-8", errors="replace")).encode("utf-8")
        blob = await self._store.put(data)
        artifact = Artifact.model_validate(
            {
                "id": new_id(IdPrefix.ARTIFACT),
                "type": type,
                "media_type": media_type,
                "sha256": blob.sha256,
                "size": blob.size,
                "path": blob.relpath,
                "provenance": Provenance.model_validate(
                    {
                        "run_id": self._rec.run.id,
                        "task_id": task_id,
                        "attempt_id": attempt_id,
                        "producer": producer,
                        "tools": tools or [],
                        "inputs": inputs or [],
                        "base_commit": base_commit,
                        "result_commit": result_commit,
                        "decisions": decisions or [],
                        "policy_hash": self._policy_hash,
                        "aix_version": self._version,
                        "created_at": self._rec.now(),
                    }
                ),
            }
        )
        await self._rec.emit(
            "artifact.created",
            ev.ArtifactCreatedPayload(artifact=artifact),
            task_id=task_id,
            attempt_id=attempt_id,
        )
        return artifact
