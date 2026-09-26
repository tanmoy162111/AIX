"""Shared streaming subprocess runner (PLAYBOOK §10.3).

* ``anyio.open_process`` in a new session/process group (stdin is /dev/null, or a pipe fed with
  ``stdin_data``); ``cwd`` is the workspace; the ``env`` is
  used *as given* (the caller has already filtered it, §20.5). Never ``shell=True``.
* stdout is delivered line by line and, optionally, mirrored to a raw capture file.
* stderr is drained concurrently into a bounded ring buffer (256 KiB tail).
* Timeout -> SIGTERM the process group -> grace -> SIGKILL.

Use as ``async with spawn(...) as proc``. ``cancel`` may be called from any task; iterating
``lines`` and calling ``wait`` belong to the task that entered the context.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import anyio
from anyio.abc import Process
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from aix.domain.errors import ToolFailure

STDERR_LIMIT = 256 * 1024
"""Bytes of stderr kept (the most recent ones)."""
MAX_LINE_BYTES = 16 * 1024 * 1024
_CHUNK = 64 * 1024


@dataclass(frozen=True)
class ProcessResult:
    """How a process ended."""

    exit_code: int | None
    """``None`` if it never started reporting; negative values are signal numbers."""
    timed_out: bool
    cancelled: bool
    stderr_tail: str
    duration_ms: int
    lines_seen: int


class RunningProcess:
    """A live child process. Created by ``spawn``."""

    def __init__(
        self,
        proc: Process,
        *,
        capture: BinaryIO | None,
        timeout_s: float,
        grace_s: float,
        send: MemoryObjectSendStream[str],
        recv: MemoryObjectReceiveStream[str],
    ) -> None:
        self._proc = proc
        self._capture = capture
        self._timeout_s = timeout_s
        self._grace_s = grace_s
        self._send = send
        self._recv = recv
        self._stderr: deque[bytes] = deque()
        self._stderr_size = 0
        self._timed_out = False
        self._cancelled = False
        self._lines_seen = 0
        self._t0 = time.monotonic()
        self._done = anyio.Event()
        self._killer_running = False

    @property
    def pid(self) -> int:
        """Process id (also the process group id)."""
        return self._proc.pid

    # ---- internal pumps -------------------------------------------------------------

    async def _pump_stdout(self) -> None:
        stream = self._proc.stdout
        assert stream is not None
        buf = bytearray()
        try:
            while True:
                try:
                    chunk = await stream.receive(_CHUNK)
                except (anyio.EndOfStream, anyio.ClosedResourceError):
                    break
                buf.extend(chunk)
                while (nl := buf.find(b"\n")) != -1:
                    self._emit(bytes(buf[:nl]))
                    del buf[: nl + 1]
                if len(buf) > MAX_LINE_BYTES:
                    self._emit(bytes(buf[:MAX_LINE_BYTES]))
                    del buf[:MAX_LINE_BYTES]
            if buf:
                self._emit(bytes(buf))
        finally:
            self._send.close()

    def _emit(self, raw: bytes) -> None:
        self._lines_seen += 1
        if self._capture is not None:
            self._capture.write(raw + b"\n")
            self._capture.flush()
        self._send.send_nowait(raw.decode("utf-8", errors="replace"))

    async def _pump_stderr(self) -> None:
        stream = self._proc.stderr
        assert stream is not None
        while True:
            try:
                chunk = await stream.receive(_CHUNK)
            except (anyio.EndOfStream, anyio.ClosedResourceError):
                return
            self._stderr.append(chunk)
            self._stderr_size += len(chunk)
            while self._stderr_size - len(self._stderr[0]) >= STDERR_LIMIT:
                self._stderr_size -= len(self._stderr.popleft())

    async def _feed_stdin(self, data: bytes) -> None:
        stream = self._proc.stdin
        assert stream is not None
        try:
            await stream.send(data)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            pass  # the child exited before reading everything; its exit code tells the story
        finally:
            await stream.aclose()

    async def _watchdog(self) -> None:
        with anyio.move_on_after(self._timeout_s) as scope:
            await self._done.wait()
        if scope.cancelled_caught and not self._done.is_set():
            self._timed_out = True
            await self._terminate(self._grace_s)

    async def _terminate(self, grace_s: float) -> None:
        """SIGTERM the process group, then SIGKILL after ``grace_s`` if it is still alive."""
        if self._proc.returncode is not None:
            return
        self._signal(signal.SIGTERM)
        with anyio.move_on_after(grace_s):
            await self._proc.wait()
        if self._proc.returncode is None:
            self._signal(signal.SIGKILL)

    def _signal(self, sig: signal.Signals) -> None:
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(self._proc.pid, sig)

    # ---- public API -----------------------------------------------------------------

    async def lines(self) -> AsyncIterator[str]:
        """Yield stdout lines (without the newline) until the stream ends."""
        async with self._recv:
            async for line in self._recv:
                yield line

    async def cancel(self, grace_s: float | None = None) -> None:
        """Terminate the process group (TERM, then KILL). Safe to call from any task."""
        self._cancelled = True
        await self._terminate(self._grace_s if grace_s is None else grace_s)

    async def wait(self) -> ProcessResult:
        """Wait for exit (discarding any unread lines) and return the result."""
        await self._proc.wait()
        self._done.set()
        self._recv.close()
        # The leader is gone; kill leftover descendants. (A recycled pgid is theoretically
        # possible but killpg only hits a *group* of that id, which a fresh pid never leads.)
        self._signal(signal.SIGKILL)
        tail = b"".join(self._stderr)[-STDERR_LIMIT:]
        return ProcessResult(
            exit_code=self._proc.returncode,
            timed_out=self._timed_out,
            cancelled=self._cancelled and not self._timed_out,
            stderr_tail=tail.decode("utf-8", errors="replace"),
            duration_ms=int((time.monotonic() - self._t0) * 1000),
            lines_seen=self._lines_seen,
        )


@asynccontextmanager
async def spawn(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
    grace_s: float = 10.0,
    capture_path: Path | None = None,
    stdin_data: bytes | None = None,
) -> AsyncIterator[RunningProcess]:
    """Start ``argv`` and manage its lifetime.

    Raises:
        ValueError: ``argv`` is empty.
        ToolFailure: the executable was not found or could not be started.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    try:
        proc = await anyio.open_process(
            argv,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=-1,
            stderr=-1,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ToolFailure(f"executable not found: {argv[0]}", details={"argv0": argv[0]}) from exc
    except OSError as exc:
        raise ToolFailure(f"cannot start {argv[0]}: {exc}", details={"argv0": argv[0]}) from exc

    capture: BinaryIO | None = None
    if capture_path is not None:
        capture_path.parent.mkdir(parents=True, exist_ok=True)
        capture = capture_path.open("wb")
    send, recv = anyio.create_memory_object_stream[str](math.inf)
    running = RunningProcess(
        proc, capture=capture, timeout_s=timeout_s, grace_s=grace_s, send=send, recv=recv
    )
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(running._pump_stdout)  # pyright: ignore[reportPrivateUsage]
            tg.start_soon(running._pump_stderr)  # pyright: ignore[reportPrivateUsage]
            tg.start_soon(running._watchdog)  # pyright: ignore[reportPrivateUsage]
            if stdin_data is not None:
                tg.start_soon(running._feed_stdin, stdin_data)  # pyright: ignore[reportPrivateUsage]
            try:
                yield running
            finally:
                # leaving the block: never leave a process behind
                with anyio.CancelScope(shield=True):
                    await running._terminate(0.0)  # pyright: ignore[reportPrivateUsage]
                    await proc.wait()
                    running._done.set()  # pyright: ignore[reportPrivateUsage]
    finally:
        with anyio.CancelScope(shield=True):
            if capture is not None:
                capture.close()
            await proc.aclose()
