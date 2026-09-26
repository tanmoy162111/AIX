from __future__ import annotations

import os
import sys
from pathlib import Path

import anyio
import pytest

from aix.verification.runner import CommandOutcome, run_command

pytestmark = pytest.mark.anyio
PY = Path(sys.executable).name
ALLOW = [PY, "sh"]


async def run(tmp: Path, code: str, **kw: object) -> CommandOutcome:
    return await run_command(
        [sys.executable, "-c", code],
        tmp,
        allow=[Path(sys.executable).name],
        timeout_s=20,
        **kw,  # type: ignore[arg-type]
    )


async def test_captures_output_and_exit_code(tmp_path: Path) -> None:
    out = await run(tmp_path, "import sys; print('hi'); print('err', file=sys.stderr); sys.exit(3)")
    assert out.status == "exited" and out.exit_code == 3
    assert out.stdout.strip() == "hi" and out.stderr.strip() == "err"
    assert out.duration_ms >= 0


async def test_runs_in_the_given_directory(tmp_path: Path) -> None:
    out = await run(tmp_path, "import os; print(os.getcwd())")
    assert Path(out.stdout.strip()).resolve() == tmp_path.resolve()


async def test_executable_outside_the_allowlist_is_blocked(tmp_path: Path) -> None:
    out = await run_command(["curl", "http://x"], tmp_path, allow=["git"], timeout_s=5)
    assert out.status == "blocked" and "curl" in out.detail


async def test_missing_tool_is_reported_not_raised(tmp_path: Path) -> None:
    out = await run_command(["no-such-tool-xyz"], tmp_path, allow=["no-such-tool-xyz"], timeout_s=5)
    assert out.status == "tool_missing"


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    child = f"import os, time; open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(60)"
    )
    out = await run_command(
        [sys.executable, "-c", code], tmp_path, allow=[PY], timeout_s=2, kill_grace_s=0.5
    )
    assert out.status == "timeout"
    pid = int(pidfile.read_text())
    await anyio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_environment_is_scrubbed_and_network_is_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SECRET_TOKEN", "leak-me")
    out = await run(
        tmp_path, "import os; print(os.environ.get('SECRET_TOKEN'), os.environ.get('HTTPS_PROXY'))"
    )
    assert out.stdout.split()[0] == "None" and "127.0.0.1" in out.stdout
    allowed = await run(
        tmp_path, "import os; print(os.environ.get('HTTPS_PROXY'))", network="allow"
    )
    assert allowed.stdout.strip() == "None"


async def test_extra_env_is_passed(tmp_path: Path) -> None:
    out = await run(tmp_path, "import os; print(os.environ['FOO'])", env={"FOO": "bar"})
    assert out.stdout.strip() == "bar"


async def test_output_is_written_to_files_and_tails_are_bounded(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out = await run(tmp_path, "print('x' * 300000)", out_dir=out_dir, name="tests")
    assert out.stdout_path is not None and out.stdout_path.stat().st_size > 300000
    assert len(out.stdout) <= 200_000 and out.stdout_path.parent == out_dir
