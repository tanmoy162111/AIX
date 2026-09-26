from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import anyio
import pytest

from aix.agents.subprocess import STDERR_LIMIT, ProcessResult, spawn
from aix.domain.errors import ToolFailure

pytestmark = pytest.mark.anyio


def py(code: str) -> list[str]:
    return [sys.executable, "-u", "-c", textwrap.dedent(code)]


def base_env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"]}


async def run_all(argv: list[str], tmp_path: Path, **kw: object) -> tuple[list[str], ProcessResult]:
    lines: list[str] = []
    kw.setdefault("timeout_s", 20)
    async with spawn(argv, cwd=tmp_path, env=base_env(), **kw) as proc:  # type: ignore[arg-type]
        async for line in proc.lines():
            lines.append(line)
        result = await proc.wait()
    return lines, result


async def test_streams_lines_in_order_and_captures_raw_output(tmp_path: Path) -> None:
    cap = tmp_path / "out" / "stream.jsonl"
    lines, res = await run_all(py("print('a'); print('b'); print('c')"), tmp_path, capture_path=cap)
    assert lines == ["a", "b", "c"]
    assert cap.read_text() == "a\nb\nc\n"
    assert res.exit_code == 0 and not res.timed_out and not res.cancelled


async def test_last_line_without_newline_and_very_long_line(tmp_path: Path) -> None:
    lines, _ = await run_all(
        py("import sys; sys.stdout.write('x'*1_000_000 + '\\n' + 'tail')"), tmp_path
    )
    assert [len(x) for x in lines] == [1_000_000, 4]
    assert lines[1] == "tail"


async def test_nonzero_exit_and_stderr_are_reported(tmp_path: Path) -> None:
    _, res = await run_all(py("import sys; sys.stderr.write('boom\\n'); sys.exit(3)"), tmp_path)
    assert res.exit_code == 3
    assert "boom" in res.stderr_tail


async def test_stderr_is_a_bounded_ring_buffer_keeping_the_tail(tmp_path: Path) -> None:
    code = """
        import sys
        for i in range(2000):
            sys.stderr.write(f"{i:04d}" + "x" * 996 + "\\n")
        sys.stderr.write("LAST\\n")
    """
    _, res = await run_all(py(code), tmp_path)
    assert len(res.stderr_tail.encode()) <= STDERR_LIMIT
    assert res.stderr_tail.rstrip().endswith("LAST")
    assert "0000x" not in res.stderr_tail  # the head was dropped


async def test_stdout_and_stderr_do_not_deadlock_when_both_are_large(tmp_path: Path) -> None:
    code = """
        import sys
        for _ in range(3000):
            sys.stdout.write("o" * 200 + "\\n")
            sys.stderr.write("e" * 200 + "\\n")
    """
    lines, res = await run_all(py(code), tmp_path)
    assert len(lines) == 3000 and res.exit_code == 0


async def test_timeout_terminates_the_process(tmp_path: Path) -> None:
    t0 = time.monotonic()
    _, res = await run_all(
        py("import time; print('hi'); time.sleep(60)"), tmp_path, timeout_s=1, grace_s=2
    )
    assert res.timed_out and not res.cancelled
    assert time.monotonic() - t0 < 8
    assert res.exit_code != 0


async def test_sigterm_ignoring_process_is_killed_after_grace(tmp_path: Path) -> None:
    code = """
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        print('ready', flush=True)
        time.sleep(60)
    """
    t0 = time.monotonic()
    _, res = await run_all(py(code), tmp_path, timeout_s=1, grace_s=1)
    assert res.timed_out
    assert 1.5 < time.monotonic() - t0 < 10


async def test_children_die_with_the_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    code = f"""
        import subprocess, sys, time
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(pidfile)!r}, "w").write(str(p.pid))
        print("spawned", flush=True)
        time.sleep(60)
    """
    await run_all(py(code), tmp_path, timeout_s=1, grace_s=1)
    child = int(pidfile.read_text())
    await anyio.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


async def test_cancel_from_another_task(tmp_path: Path) -> None:
    async with spawn(
        py("import time; print('up', flush=True); time.sleep(60)"),
        cwd=tmp_path,
        env=base_env(),
        timeout_s=60,
    ) as proc:
        async with anyio.create_task_group() as tg:

            async def cancel_soon() -> None:
                await anyio.sleep(0.5)
                await proc.cancel(grace_s=1)

            tg.start_soon(cancel_soon)
            async for _ in proc.lines():
                pass
        res = await proc.wait()
    assert res.cancelled and not res.timed_out


async def test_no_shell_interpretation(tmp_path: Path) -> None:
    lines, _ = await run_all(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "a;b $HOME `x`"], tmp_path
    )
    assert lines == ["a;b $HOME `x`"]


async def test_cwd_and_exact_environment(tmp_path: Path) -> None:
    (tmp_path / "w").mkdir()
    code = "import os, json; print(os.getcwd()); print(json.dumps(sorted(os.environ)))"
    lines: list[str] = []
    async with spawn(
        py(code), cwd=tmp_path / "w", env={**base_env(), "ONLY": "1"}, timeout_s=20
    ) as p:
        async for line in p.lines():
            lines.append(line)
    assert lines[0] == str((tmp_path / "w").resolve())
    assert '"ONLY"' in lines[1] and "HOME" not in lines[1]


async def test_missing_binary_is_a_typed_error(tmp_path: Path) -> None:
    with pytest.raises(ToolFailure, match="not found"):
        async with spawn(
            ["definitely-not-a-binary-xyz"], cwd=tmp_path, env=base_env(), timeout_s=5
        ):
            pass


async def test_empty_argv_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        async with spawn([], cwd=tmp_path, env=base_env(), timeout_s=5):
            pass


async def test_wait_without_consuming_lines_still_finishes(tmp_path: Path) -> None:
    async with spawn(py("print('x')"), cwd=tmp_path, env=base_env(), timeout_s=20) as p:
        res = await p.wait()
    assert res.exit_code == 0


async def test_stdin_is_devnull_by_default_and_fed_when_given(tmp_path: Path) -> None:
    code = "import sys; print(repr(sys.stdin.read()))"
    lines, _ = await run_all(py(code), tmp_path)
    assert lines == ["''"]
    big = "x" * 300_000
    lines2, res = await run_all(
        py("import sys; print(len(sys.stdin.read()))"), tmp_path, stdin_data=big.encode()
    )
    assert lines2 == ["300000"] and res.exit_code == 0
