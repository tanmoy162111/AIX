"""Ollama adapter against a scripted local HTTP server (M8.6, PLAYBOOK §11)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import anyio
import pytest

from aix.agents.adapters.ollama import OllamaAdapter, parse_host
from aix.agents.protocol import AgentRequest
from aix.domain.enums import FailureClass

pytestmark = pytest.mark.anyio


class Script:
    """What the fake server does; mutated per test."""

    models: ClassVar[list[str]] = ["llama3", "qwen"]
    chunks: ClassVar[list[str]] = ["Hello", " world"]
    status: ClassVar[int] = 200
    delay_s: ClassVar[float] = 0.0
    seen: ClassVar[list[dict[str, object]]] = []


def make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            body = json.dumps({"models": [{"name": m} for m in Script.models]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            Script.seen.append(json.loads(self.rfile.read(length)))
            if Script.status != 200:
                body = b'{"error":"model not found"}'
                self.send_response(Script.status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            try:
                for c in Script.chunks:
                    time.sleep(Script.delay_s)
                    self.wfile.write(
                        json.dumps({"message": {"content": c}, "done": False}).encode() + b"\n"
                    )
                    self.wfile.flush()
                done = {
                    "done": True,
                    "prompt_eval_count": 11,
                    "eval_count": 7,
                    "message": {"content": ""},
                }
                self.wfile.write(json.dumps(done).encode() + b"\n")
            except (BrokenPipeError, ConnectionResetError):
                pass

    return Handler


@pytest.fixture
def server() -> Iterator[str]:
    Script.models, Script.chunks, Script.status, Script.delay_s = (
        ["llama3", "qwen"],
        ["Hello", " world"],
        200,
        0.0,
    )
    Script.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def request(tmp_path: Path, host: str, **kw: object) -> AgentRequest:
    base: dict[str, object] = {
        "attempt_id": "att_01ARZ3NDEKTSV4RRFFQ69G5FAV", "workspace": tmp_path, "prompt": "hi",
        "timeout_s": 10, "env": {"OLLAMA_HOST": host},
    }  # fmt: skip
    return AgentRequest.model_validate({**base, **kw})


async def run(adapter: OllamaAdapter, req: AgentRequest):  # type: ignore[no-untyped-def]
    h = await adapter.start(req)
    events = [e async for e in adapter.events(h)]
    return events, await adapter.wait(h)


def test_parse_host_forms() -> None:
    assert parse_host(None) == ("127.0.0.1", 11434, False)
    assert parse_host("box:9999") == ("box", 9999, False)
    assert parse_host("https://ollama.example") == ("ollama.example", 443, True)


async def test_probe_lists_models_and_reports_unreachable(server: str) -> None:
    spec = await OllamaAdapter(env_source={"OLLAMA_HOST": server}).probe()
    assert (
        spec.health == "ready"
        and spec.models == ["llama3", "qwen"]
        and spec.default_model == "llama3"
    )
    assert spec.supports.enforces == ["read_only", "write_scope", "network_deny"]
    gone = await OllamaAdapter(env_source={"OLLAMA_HOST": "127.0.0.1:1"}).probe()
    assert gone.health == "unavailable" and "cannot reach ollama" in (gone.health_reason or "")
    Script.models = []
    assert (await OllamaAdapter(env_source={"OLLAMA_HOST": server}).probe()).health == "degraded"


async def test_streams_text_and_reports_usage_without_touching_the_workspace(
    server: str, tmp_path: Path
) -> None:
    adapter = OllamaAdapter(env_source={"OLLAMA_HOST": server})
    await adapter.probe()
    events, outcome = await run(adapter, request(tmp_path, server))
    kinds = [e.kind for e in events]
    assert kinds == ["started", "text", "text", "usage", "finished"]
    assert outcome.status == "completed" and outcome.claim == "Hello world"
    assert outcome.usage.input_tokens == 11 and outcome.usage.output_tokens == 7
    assert outcome.usage.cost_usd == 0.0 and outcome.usage.model == "llama3"
    assert list(tmp_path.iterdir()) == []  # text-only: never writes files
    assert Script.seen[0]["model"] == "llama3" and Script.seen[0]["stream"] is True


async def test_explicit_model_wins(server: str, tmp_path: Path) -> None:
    adapter = OllamaAdapter(env_source={"OLLAMA_HOST": server})
    await run(adapter, request(tmp_path, server, model="qwen"))
    assert Script.seen[0]["model"] == "qwen"


async def test_failures_are_classified(server: str, tmp_path: Path) -> None:
    adapter = OllamaAdapter(env_source={"OLLAMA_HOST": server})
    Script.status = 404
    _, missing = await run(adapter, request(tmp_path, server, model="nope"))
    assert missing.status == "failed" and missing.failure is FailureClass.AGENT_FAILURE
    Script.status = 503
    _, down = await run(adapter, request(tmp_path, server, model="x"))
    assert down.failure is FailureClass.NETWORK_FAILURE
    _, unreachable = await run(adapter, request(tmp_path, "127.0.0.1:1", model="x"))
    assert unreachable.failure is FailureClass.NETWORK_FAILURE
    _, no_model = await run(adapter, request(tmp_path, server))  # never probed: no model known
    assert no_model.failure is FailureClass.AGENT_FAILURE


async def test_timeout(server: str, tmp_path: Path) -> None:
    Script.delay_s, Script.chunks = 2.0, ["slow"]
    adapter = OllamaAdapter(env_source={"OLLAMA_HOST": server})
    _, outcome = await run(adapter, request(tmp_path, server, model="x", timeout_s=1))
    assert outcome.status == "timeout" and outcome.failure is FailureClass.TIMEOUT


async def test_cancel_mid_stream(server: str, tmp_path: Path) -> None:
    Script.delay_s, Script.chunks = 0.3, ["a"] * 20
    adapter = OllamaAdapter(env_source={"OLLAMA_HOST": server})
    h = await adapter.start(request(tmp_path, server, model="x"))
    seen: list[str] = []

    async def drain() -> None:
        async for e in adapter.events(h):
            seen.append(e.kind)

    async with anyio.create_task_group() as tg:
        tg.start_soon(drain)
        await anyio.sleep(0.8)
        await adapter.cancel(h)
    outcome = await adapter.wait(h)
    assert outcome.status == "cancelled" and seen[-1] == "finished" and len(seen) < 20
