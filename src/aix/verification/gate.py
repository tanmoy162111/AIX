"""Command gate type shared by the baseline runner and the engine."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

CommandGate = Callable[[list[str]], Awaitable[str | None]]
"""Checks a control-plane command before it runs; returns a refusal reason, or ``None`` to allow."""
