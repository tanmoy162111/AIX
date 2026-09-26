"""Run verification commands in an attempt worktree (PLAYBOOK §17.2, §20).

Never uses a shell. The executable must be on the allowlist. The environment is a minimal base
plus explicit extras (no inherited secrets, §20.5). ``network: deny`` is best effort in local mode:
proxies point at a closed port and package managers are told to stay offline; a container sandbox
enforces it properly (§20.3). On timeout the whole process group is terminated.
"""

from __future__ import annotations

import os
import signal
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

import anyio

from aix.security.redact import redact_secrets

TAIL_BYTES: Final = 200_000
_BASE_ENV: Final = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")
_OFFLINE: Final = {
    "HTTP_PROXY": "http://127.0.0.1:9",
    "HTTPS_PROXY": "http://127.0.0.1:9",
    "ALL_PROXY": "http://127.0.0.1:9",
    "NO_PROXY": "",
    "npm_config_offline": "true",
    "CARGO_NET_OFFLINE": "true",
    "PIP_NO_INDEX": "1",
    "GOFLAGS": "-mod=mod",
    "GOPROXY": "off",
}


@dataclass(frozen=True)
class CommandOutcome:
    """How a command ended. ``stdout``/``stderr`` are bounded tails; the files hold everything."""

    status: Literal["exited", "timeout", "blocked", "tool_missing"]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    detail: str = ""
    stdout_path: Path | None = None
    stderr_path: Path | None = None


def _env(extra: dict[str, str] | None, network: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in _BASE_ENV if k in os.environ}
    if network != "allow":
        env.update(_OFFLINE)
    env.update(extra or {})
    return env


def _tail(data: bytes) -> str:
    return data[-TAIL_BYTES:].decode("utf-8", errors="replace")


async def _read_all(stream: anyio.abc.ByteReceiveStream | None, sink: list[bytes]) -> None:
    if stream is None:
        return
    with suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
        async for chunk in stream:
            sink.append(chunk)


def _killpg(pid: int, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def run_command(
    argv: list[str],
    cwd: Path,
    *,
    allow: list[str],
    timeout_s: float,
    env: dict[str, str] | None = None,
    network: str = "deny",
    out_dir: Path | None = None,
    name: str = "check",
    kill_grace_s: float = 5.0,
) -> CommandOutcome:
    """Run ``argv`` in ``cwd`` and capture its output.

    Contract: never raises for command problems. ``blocked`` when ``argv[0]``'s basename is not in
    ``allow``; ``tool_missing`` when it cannot be started; ``timeout`` after ``timeout_s`` (the
    process group gets SIGTERM, then SIGKILL after ``kill_grace_s``). With ``out_dir`` the full
    stdout/stderr are written to ``<name>.stdout.txt`` / ``<name>.stderr.txt``.
    """
    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    exe = Path(argv[0]).name
    if exe not in allow:
        return CommandOutcome("blocked", None, "", "", 0, f"{exe!r} is not in the shell allowlist")
    try:
        proc = await anyio.open_process(
            argv,
            cwd=cwd,
            env=_env(env, network),
            stdin=None,
            start_new_session=True,
        )
    except (FileNotFoundError, PermissionError) as exc:
        return CommandOutcome("tool_missing", None, "", "", elapsed(), f"cannot start {exe}: {exc}")

    out: list[bytes] = []
    err: list[bytes] = []
    timed_out = False
    async with proc, anyio.create_task_group() as tg:
        tg.start_soon(_read_all, proc.stdout, out)
        tg.start_soon(_read_all, proc.stderr, err)
        with anyio.move_on_after(timeout_s) as scope:
            await proc.wait()
        if scope.cancelled_caught:
            timed_out = True
            _killpg(proc.pid, signal.SIGTERM)
            with anyio.move_on_after(kill_grace_s):
                await proc.wait()
            _killpg(proc.pid, signal.SIGKILL)
            with anyio.CancelScope(shield=True), anyio.move_on_after(2):
                await proc.wait()
        else:
            _killpg(proc.pid, signal.SIGKILL)  # stragglers holding the pipes open
    stdout = redact_secrets(b"".join(out).decode("utf-8", errors="replace")).encode("utf-8")
    stderr = redact_secrets(b"".join(err).decode("utf-8", errors="replace")).encode("utf-8")
    out_path = err_path = None
    if out_dir is not None:
        await anyio.Path(out_dir).mkdir(parents=True, exist_ok=True)
        out_path, err_path = out_dir / f"{name}.stdout.txt", out_dir / f"{name}.stderr.txt"
        await anyio.Path(out_path).write_bytes(stdout)
        await anyio.Path(err_path).write_bytes(stderr)
    return CommandOutcome(
        "timeout" if timed_out else "exited",
        None if timed_out else proc.returncode,
        _tail(stdout),
        _tail(stderr),
        elapsed(),
        f"timed out after {timeout_s:g}s" if timed_out else "",
        out_path,
        err_path,
    )
