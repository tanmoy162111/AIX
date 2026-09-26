"""Helpers shared by CLI commands."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer

from aix.agents.registry import AdapterRegistry
from aix.config.loader import ResolvedConfig, load_config
from aix.domain.errors import ConfigError

EXIT_USAGE = 2
EXIT_ENVIRONMENT = 5


def fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    """Print an error to stderr and exit with ``code`` (PLAYBOOK §23.2)."""
    typer.echo(f"error: {message}", err=True)
    raise typer.Exit(code)


def load_or_exit(project: Path) -> ResolvedConfig:
    """Load the layered config or exit with the usage/config error code."""
    try:
        return load_config(project.resolve())
    except ConfigError as exc:
        fail(str(exc))


def registry_or_exit(resolved: ResolvedConfig) -> AdapterRegistry:
    """Build the adapter registry from config or exit on a broken adapter."""
    try:
        return AdapterRegistry(resolved.config)
    except ConfigError as exc:
        fail(str(exc))
