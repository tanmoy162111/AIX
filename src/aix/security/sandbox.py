"""Container sandbox readiness and configuration (PLAYBOOK §20.3, ADR-0029)."""

from __future__ import annotations

import os
import shutil

import anyio

from aix.agents.protocol import ContainerSpec
from aix.config.schema import SecurityConfig
from aix.domain.errors import ToolFailure

REASON = "container"


def detect_runtime(preferred: str = "auto") -> str | None:
    """``docker`` or ``podman`` when installed (``preferred`` narrows the search), else ``None``."""
    order = ("docker", "podman") if preferred == "auto" else (preferred,)
    return next((r for r in order if shutil.which(r)), None)


def container_spec(security: SecurityConfig) -> ContainerSpec | None:
    """The container to run agents in, or ``None`` in ``local`` mode.

    Raises:
        ToolFailure: container mode without a runtime or an image (reason ``container``).
    """
    if security.sandbox != "container":
        return None
    cfg = security.container
    runtime = detect_runtime(cfg.runtime)
    if runtime is None:
        raise ToolFailure(
            "security.sandbox is 'container' but no container runtime (docker or podman) was "
            "found; install one or set security.sandbox: local",
            details={"reason": REASON},
        )
    if not cfg.image:
        raise ToolFailure(
            "security.sandbox is 'container' but security.container.image is not set; name an "
            "image that contains your agent CLI",
            details={"reason": REASON},
        )
    return ContainerSpec(
        runtime=runtime,  # type: ignore[arg-type]
        image=cfg.image,
        network=cfg.network,
        memory=cfg.memory,
        pids_limit=cfg.pids_limit,
        uid=os.getuid(),
        gid=os.getgid(),
    )


async def ensure_container_ready(security: SecurityConfig) -> ContainerSpec | None:
    """Like :func:`container_spec`, and also check that the runtime answers and has the image.

    Raises:
        ToolFailure: anything missing, with a message saying what to fix (reason ``container``).
    """
    spec = container_spec(security)
    if spec is None:
        return None
    for args, problem in (
        (["info"], f"{spec.runtime} is installed but its daemon does not answer"),
        (["image", "inspect", spec.image], f"image {spec.image!r} is not available locally"),
    ):
        try:
            res = await anyio.run_process([spec.runtime, *args], check=False)
        except OSError as exc:
            raise ToolFailure(
                f"cannot run {spec.runtime}: {exc}", details={"reason": REASON}
            ) from exc
        if res.returncode != 0:
            raise ToolFailure(problem, details={"reason": REASON})
    return spec
