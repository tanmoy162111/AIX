"""Smoke tests for the CLI entry point (M0.8)."""

from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version

from typer.testing import CliRunner

from aix import __version__
from aix.cli.main import app

runner = CliRunner()


def test_version_flag_prints_package_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"aix {version('aix')}"


def test_dunder_version_matches_metadata() -> None:
    assert __version__ == version("aix")


def test_help_lists_program_description() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Universal AI Agent Control Plane" in result.stdout


def test_unknown_command_is_a_usage_error() -> None:
    result = runner.invoke(app, ["definitely-not-a-command"])
    assert result.exit_code == 2  # PLAYBOOK §23.2: usage/config error


def test_console_script_module_runs_help() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "from aix.cli.main import app; app(['--help'])"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0
    assert "Usage" in proc.stdout
