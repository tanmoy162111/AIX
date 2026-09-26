"""Helpers for placing fake agent binaries on PATH in tests."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path


def make_fake_binary(bin_dir: Path, name: str, python_body: str) -> Path:
    """Write an executable Python script ``bin_dir/name`` and return its path."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(python_body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def env_with_path(bin_dir: Path, **extra: str) -> dict[str, str]:
    """A minimal env whose PATH starts with ``bin_dir``."""
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", **extra}


def make_replay_binary(
    bin_dir: Path,
    name: str,
    *,
    recording: Path | None = None,
    exit_code: int = 0,
    stderr: str = "",
    sleep_s: float = 0.0,
    version: str = "9.9.9",
    help_text: str = "",
    log_dir: Path | None = None,
) -> Path:
    """A fake agent CLI: answers --version/--help and replays a recorded stream on any other call.

    When ``log_dir`` is given it writes ``argv.json`` and ``stdin.txt`` there so tests can assert
    exactly how the adapter invoked it.
    """
    rec = str(recording) if recording else ""
    return make_fake_binary(
        bin_dir,
        name,
        f"""
        import json, os, sys, time
        args = sys.argv[1:]
        if "--version" in args:
            print({version!r}); sys.exit(0)
        if "--help" in args:
            print({help_text!r}); sys.exit(0)
        log = {str(log_dir) if log_dir else ""!r}
        stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
        if log:
            os.makedirs(log, exist_ok=True)
            open(os.path.join(log, "argv.json"), "w").write(json.dumps(args))
            open(os.path.join(log, "stdin.txt"), "w").write(stdin)
            open(os.path.join(log, "env.json"), "w").write(json.dumps(sorted(os.environ)))
            open(os.path.join(log, "cwd.txt"), "w").write(os.getcwd())
        rec = {rec!r}
        if rec:
            for line in open(rec):
                sys.stdout.write(line if line.endswith("\\n") else line + "\\n"); sys.stdout.flush()
        if {stderr!r}:
            sys.stderr.write({stderr!r}); sys.stderr.flush()
        if {sleep_s!r}:
            time.sleep({sleep_s!r})
        sys.exit({exit_code!r})
        """,
    )
