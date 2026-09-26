"""Wrap an agent command line so it runs in a container (PLAYBOOK §20.3, ADR-0029)."""

from __future__ import annotations

from collections.abc import Mapping

from aix.agents.protocol import AgentRequest, ContainerSpec

MOUNT: str = "/workspace"
_NOT_FORWARDED = frozenset({"PATH", "HOME", "PWD", "OLDPWD", "SHLVL", "_"})


def container_name(req: AgentRequest) -> str:
    return f"aix-{req.attempt_id}"


def wrap_command(
    argv: list[str], req: AgentRequest, env: Mapping[str, str]
) -> tuple[list[str], dict[str, str]]:
    """``(container argv, environment for the runtime client)`` for ``argv``.

    Contract: only ``req.workspace`` is mounted (read-write, at ``/workspace``); no home
    directory, no aix database, no approval token. The container runs as the caller's uid/gid with
    every capability dropped, ``no-new-privileges``, a read-only root filesystem and tmpfs for
    ``/tmp`` and ``$HOME``. Credentials are forwarded by *name* (``-e NAME``): their values
    travel in the runtime client's environment, never in argv (where ``ps`` would show them).
    The host ``PATH``/``HOME`` are not forwarded, since they mean nothing inside the image.

    Raises:
        ValueError: the request has no ``container`` spec.
    """
    spec: ContainerSpec | None = req.container
    if spec is None:
        raise ValueError("request has no container spec")
    forwarded = sorted(k for k in env if k not in _NOT_FORWARDED)
    out = [
        spec.runtime, "run", "--rm", "-i", "--init",
        "--name", container_name(req),
        "--network", spec.network,
        "--user", f"{spec.uid}:{spec.gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=1g",
        "--tmpfs", "/home/agent:rw,nosuid,size=512m",
        "--pids-limit", str(spec.pids_limit),
        "-e", "HOME=/home/agent",
        "-v", f"{req.workspace}:{MOUNT}:rw",
        "-w", MOUNT,
    ]  # fmt: skip
    if spec.memory:
        out += ["--memory", spec.memory]
    for name in forwarded:
        out += ["-e", name]
    out += [spec.image, *argv]
    client_env = {k: v for k, v in env.items() if k in forwarded or k in ("PATH", "HOME")}
    return out, client_env


async def kill_container(runtime: str, name: str) -> None:
    """Best-effort ``<runtime> rm -f <name>``; never raises."""
    import contextlib

    import anyio

    with contextlib.suppress(OSError):
        await anyio.run_process([runtime, "rm", "-f", name], check=False)
