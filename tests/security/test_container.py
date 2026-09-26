"""Container sandbox: command wrapping, readiness, and a real container run (M8.4, §20.3)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from aix.agents.container import MOUNT, container_name, wrap_command
from aix.agents.protocol import AgentRequest, ContainerSpec
from aix.config.schema import ContainerConfig, SecurityConfig
from aix.domain.errors import ToolFailure
from aix.security.sandbox import container_spec, detect_runtime, ensure_container_ready

pytestmark = pytest.mark.anyio


def spec(**kw: object) -> ContainerSpec:
    base: dict[str, object] = {"runtime": "docker", "image": "img:1", "uid": 1000, "gid": 1000}
    return ContainerSpec.model_validate({**base, **kw})


def request(ws: Path, container: ContainerSpec | None = None) -> AgentRequest:
    return AgentRequest(
        attempt_id="att_01ARZ3NDEKTSV4RRFFQ69G5FAV", workspace=ws, prompt="p", timeout_s=30,
        container=container,
    )  # fmt: skip


def test_wrap_mounts_only_the_workspace_and_drops_privileges(tmp_path: Path) -> None:
    env = {"PATH": "/usr/bin", "HOME": "/home/me", "OPENAI_API_KEY": "sekret-value-123"}
    argv, client_env = wrap_command(["codex", "exec", "-"], request(tmp_path, spec()), env)
    assert argv[:3] == ["docker", "run", "--rm"] and argv[-3:] == ["codex", "exec", "-"]
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "1000:1000"
    assert argv[argv.index("--cap-drop") + 1] == "ALL" and "--read-only" in argv
    assert "no-new-privileges" in argv
    mounts = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert mounts == [f"{tmp_path}:{MOUNT}:rw"]  # nothing else: no home, no .aix, no token
    assert argv[argv.index("--name") + 1] == container_name(request(tmp_path))
    joined = " ".join(argv)
    assert "sekret-value-123" not in joined  # forwarded by name, never by value
    assert "OPENAI_API_KEY" in argv and "/home/me" not in joined
    assert client_env["OPENAI_API_KEY"] == "sekret-value-123" and client_env["PATH"] == "/usr/bin"


def test_wrap_requires_a_spec_and_honours_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        wrap_command(["x"], request(tmp_path), {})
    argv, _ = wrap_command(
        ["x"], request(tmp_path, spec(memory="2g", pids_limit=64, network="bridge")), {}
    )
    assert argv[argv.index("--memory") + 1] == "2g" and argv[argv.index("--pids-limit") + 1] == "64"
    assert argv[argv.index("--network") + 1] == "bridge"


def test_local_mode_has_no_container() -> None:
    assert container_spec(SecurityConfig()) is None


def test_container_mode_needs_runtime_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = SecurityConfig(sandbox="container", container=ContainerConfig(image="img:1"))
    monkeypatch.setattr("aix.security.sandbox.shutil.which", lambda _: None)
    with pytest.raises(ToolFailure, match="no container runtime") as exc:
        container_spec(cfg)
    assert exc.value.details["reason"] == "container"
    monkeypatch.setattr("aix.security.sandbox.shutil.which", lambda n: f"/bin/{n}")
    with pytest.raises(ToolFailure, match="image is not set"):
        container_spec(SecurityConfig(sandbox="container"))
    got = container_spec(cfg)
    assert got is not None and got.runtime == "docker" and got.uid == os.getuid()
    assert detect_runtime("podman") == "podman"


def _docker_up() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_up(), reason="no running docker daemon")
async def test_readiness_reports_a_missing_image() -> None:
    cfg = SecurityConfig(
        sandbox="container",
        container=ContainerConfig(runtime="docker", image="aix-no-such-image:never"),
    )
    with pytest.raises(ToolFailure, match="not available locally"):
        await ensure_container_ready(cfg)


def _docker_has(image: str) -> bool:
    if not shutil.which("docker"):
        return False
    return (
        subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    )


@pytest.mark.skipif(not _docker_has("bash:latest"), reason="docker with bash:latest not available")
def test_real_container_sees_only_the_workspace_and_no_network(tmp_path: Path) -> None:
    (tmp_path / "hello.txt").write_text("hi")
    req = request(tmp_path, spec(image="bash:latest", uid=os.getuid(), gid=os.getgid()))
    script = (
        "cat hello.txt; echo -n ' '; echo ok > written.txt; "
        "ls /home 2>/dev/null | tr '\\n' ' '; "
        "touch /etc/nope 2>/dev/null && echo ROOTFS_WRITABLE; "
        "(echo > /dev/tcp/1.1.1.1/80) 2>/dev/null && echo NETWORK_UP; "
        "echo ENV=${SECRET_TOKEN:-unset}"
    )
    argv, env = wrap_command(["bash", "-c", script], req, {"SECRET_TOKEN": "s3cret-forwarded"})
    res = subprocess.run(
        argv, env={**os.environ, **env}, capture_output=True, text=True, timeout=120
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("hi ") and "ROOTFS_WRITABLE" not in res.stdout
    assert "NETWORK_UP" not in res.stdout and "ENV=s3cret-forwarded" in res.stdout
    assert (tmp_path / "written.txt").read_text().strip() == "ok"  # the workspace is writable
    assert "s3cret-forwarded" not in " ".join(argv)
