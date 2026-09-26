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
