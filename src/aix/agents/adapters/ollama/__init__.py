"""Ollama adapter: text-only local models over HTTP (PLAYBOOK §11, §20; docs/adapters/ollama.md).

``POST /api/chat`` streams NDJSON. The model only produces text, so the adapter never touches the
workspace: it can serve ``research``, ``review`` and ``summarize`` tasks, and cannot edit files.
The prompt is sent as one user message; the reply is the claim (unverified, like any claim).
Blocking ``http.client`` calls run in worker threads so the event loop is never blocked.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import os
import socket
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

import anyio
import yaml

from aix.agents.manifest import AdapterManifest
from aix.agents.protocol import AgentAdapter, AgentEvent, AgentHandle, AgentOutcome, AgentRequest
from aix.domain.agents import AgentSpec
from aix.domain.enums import FailureClass
from aix.domain.errors import UnsupportedError
from aix.domain.execution import Usage

DEFAULT_HOST = "http://127.0.0.1:11434"
PROBE_TIMEOUT_S = 3.0


def load_manifest() -> AdapterManifest:
    text = resources.files(__package__).joinpath("manifest.yaml").read_text(encoding="utf-8")
    return AdapterManifest.model_validate(yaml.safe_load(text))


def parse_host(value: str | None) -> tuple[str, int, bool]:
    """``(host, port, tls)`` from ``OLLAMA_HOST`` (``host``, ``host:port`` or a full URL)."""
    raw = (value or DEFAULT_HOST).strip()
    if "://" not in raw:
        raw = f"http://{raw}"
    parts = urlsplit(raw)
    tls = parts.scheme == "https"
    return parts.hostname or "127.0.0.1", parts.port or (443 if tls else 11434), tls


def _now() -> datetime:
    return datetime.now(UTC)


def _connect(host: tuple[str, int, bool], timeout: float) -> http.client.HTTPConnection:
    name, port, tls = host
    cls = http.client.HTTPSConnection if tls else http.client.HTTPConnection
    return cls(name, port, timeout=timeout)


@dataclass
class _Run:
    req: AgentRequest
    model: str
    conn: http.client.HTTPConnection | None = None
    text: list[str] = field(default_factory=list[str])
    usage: Usage = field(default_factory=Usage)
    outcome: AgentOutcome | None = None
    cancelled: bool = False


class OllamaAdapter:
    """Drives a local Ollama server. Health is ``unavailable`` when it does not answer."""

    id = "ollama"

    def __init__(
        self,
        manifest: AdapterManifest | None = None,
        *,
        env_source: Mapping[str, str] | None = None,
    ) -> None:
        self.manifest = manifest or load_manifest()
        self._env = env_source
        self._models: list[str] = []

    def _host(self, extra: Mapping[str, str] | None = None) -> tuple[str, int, bool]:
        src = os.environ if self._env is None else self._env
        return parse_host((extra or {}).get("OLLAMA_HOST") or src.get("OLLAMA_HOST"))

    # ---- protocol ---------------------------------------------------------------------

    async def probe(self) -> AgentSpec:
        m = self.manifest
        health, reason, models = "ready", None, []
        host = self._host()
        try:
            models = await anyio.to_thread.run_sync(self._list_models, host)
            self._models = models
            if not models:
                health, reason = "degraded", "no models pulled (run `ollama pull <model>`)"
        except (OSError, http.client.HTTPException, ValueError) as exc:
            health, reason = "unavailable", f"cannot reach ollama at {host[0]}:{host[1]}: {exc}"
        return AgentSpec(
            id=m.id,
            name=m.name,
            kind=m.kind,
            capabilities=m.capabilities,
            supports=m.supports,
            models=models,
            default_model=models[0] if models else None,
            cost_class=m.cost_class,
            health=health,
            health_reason=reason,
        )  # type: ignore[arg-type]

    @staticmethod
    def _list_models(host: tuple[str, int, bool]) -> list[str]:
        conn = _connect(host, PROBE_TIMEOUT_S)
        try:
            conn.request("GET", "/api/tags")
            resp = conn.getresponse()
            if resp.status != 200:
                raise http.client.HTTPException(f"HTTP {resp.status}")
            doc: Any = json.loads(resp.read())
            return [str(x["name"]) for x in doc.get("models", []) if isinstance(x, dict)]
        finally:
            conn.close()

    async def start(self, req: AgentRequest) -> AgentHandle:
        model = req.model or (self._models[0] if self._models else "")
        return AgentHandle(
            attempt_id=req.attempt_id, adapter_id=self.id, state=_Run(req=req, model=model)
        )

    async def events(self, h: AgentHandle) -> AsyncIterator[AgentEvent]:
        run: _Run = h.state
        yield AgentEvent(kind="started", ts=_now())
        if not run.model:
            run.outcome = AgentOutcome(
                exit_code=1, status="failed", failure=FailureClass.AGENT_FAILURE,
                stderr_tail="no model given and none available; pass a model or pull one",
            )  # fmt: skip
            yield AgentEvent(kind="error", ts=_now(), data={"stderr": run.outcome.stderr_tail})
            yield AgentEvent(kind="finished", ts=_now(), data={"status": "failed"})
            return
        host = self._host(run.req.env)
        body = json.dumps(
            {"model": run.model, "stream": True,
             "messages": [{"role": "user", "content": run.req.prompt}]}
        ).encode()  # fmt: skip
        try:
            with anyio.fail_after(run.req.timeout_s):
                conn = await anyio.to_thread.run_sync(self._open, host, run.req.timeout_s, body)
                run.conn = conn
                resp = await anyio.to_thread.run_sync(conn.getresponse)
                if resp.status != 200:
                    detail = (await anyio.to_thread.run_sync(resp.read))[:300].decode(
                        errors="replace"
                    )
                    failure = (
                        FailureClass.AGENT_FAILURE
                        if resp.status == 404
                        else FailureClass.NETWORK_FAILURE
                    )
                    run.outcome = AgentOutcome(
                        exit_code=1, status="failed", failure=failure,
                        stderr_tail=f"ollama HTTP {resp.status}: {detail}",
                    )  # fmt: skip
                    yield AgentEvent(
                        kind="error", ts=_now(), data={"stderr": run.outcome.stderr_tail}
                    )
                    yield AgentEvent(kind="finished", ts=_now(), data={"status": "failed"})
                    return
                while True:
                    line = await anyio.to_thread.run_sync(resp.readline)
                    if run.cancelled:
                        run.outcome = AgentOutcome(exit_code=None, status="cancelled")
                        break
                    if not line:
                        break
                    chunk: Any = json.loads(line)
                    piece = (chunk.get("message") or {}).get("content") or ""
                    if piece:
                        run.text.append(piece)
                        yield AgentEvent(kind="text", ts=_now(), data={"text": piece})
                    if chunk.get("error"):
                        raise http.client.HTTPException(str(chunk["error"]))
                    if chunk.get("done"):
                        run.usage = Usage(
                            input_tokens=chunk.get("prompt_eval_count"),
                            output_tokens=chunk.get("eval_count"),
                            cost_usd=0.0, model=run.model,
                        )  # fmt: skip
                        yield AgentEvent(
                            kind="usage", ts=_now(), data=run.usage.model_dump(mode="json")
                        )
                        run.outcome = AgentOutcome(
                            exit_code=0,
                            status="completed",
                            claim="".join(run.text),
                            usage=run.usage,
                        )
                        break
            if run.outcome is None:
                run.outcome = AgentOutcome(
                    exit_code=1, status="failed", failure=FailureClass.AGENT_FAILURE,
                    stderr_tail="stream ended before the final message",
                )  # fmt: skip
        except TimeoutError:
            run.outcome = AgentOutcome(
                exit_code=None, status="timeout", failure=FailureClass.TIMEOUT
            )
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if run.cancelled:
                run.outcome = AgentOutcome(exit_code=None, status="cancelled")
            else:
                run.outcome = AgentOutcome(
                    exit_code=1, status="failed", failure=FailureClass.NETWORK_FAILURE,
                    stderr_tail=f"ollama request failed: {exc}",
                )  # fmt: skip
        finally:
            if run.conn is not None:
                run.conn.close()
        assert run.outcome is not None
        if run.outcome.status == "failed":
            yield AgentEvent(kind="error", ts=_now(), data={"stderr": run.outcome.stderr_tail})
        yield AgentEvent(kind="finished", ts=_now(), data={"status": run.outcome.status})

    @staticmethod
    def _open(
        host: tuple[str, int, bool], timeout: float, body: bytes
    ) -> http.client.HTTPConnection:
        conn = _connect(host, timeout)
        conn.request("POST", "/api/chat", body=body, headers={"Content-Type": "application/json"})
        return conn

    async def wait(self, h: AgentHandle) -> AgentOutcome:
        run: _Run = h.state
        if run.outcome is None:
            async for _ in self.events(h):
                pass
        assert run.outcome is not None
        return run.outcome

    async def cancel(self, h: AgentHandle, grace_s: float = 10) -> None:
        run: _Run = h.state
        run.cancelled = True
        if run.conn is not None:  # a shutdown (not just close) unblocks a read in the worker thread
            with contextlib.suppress(OSError):
                if run.conn.sock is not None:
                    run.conn.sock.shutdown(socket.SHUT_RDWR)
            run.conn.close()
        if run.outcome is None:
            run.outcome = AgentOutcome(exit_code=None, status="cancelled")

    async def resume(self, session_ref: str, req: AgentRequest) -> AgentHandle:
        raise UnsupportedError("ollama has no sessions")


def create() -> AgentAdapter:
    """Factory used by the registry."""
    return OllamaAdapter()
