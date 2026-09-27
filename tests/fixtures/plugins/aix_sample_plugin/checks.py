"""The sample check plugin: fails a change that adds a ``TODO``."""

from __future__ import annotations

from aix.verification.plugins import CheckContext, PluginCheckResult


async def no_todo(ctx: CheckContext) -> PluginCheckResult:
    added = [
        line[1:]
        for line in ctx.patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    todos = [line for line in added if "TODO" in line]
    if todos:
        return PluginCheckResult(
            "failed", f"{len(todos)} added line(s) contain TODO", {"todos": len(todos)}
        )
    return PluginCheckResult("passed", "no TODO added", {"todos": 0})
