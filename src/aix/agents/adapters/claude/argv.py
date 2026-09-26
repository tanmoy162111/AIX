"""Translate an ``AgentRequest`` into Claude Code's headless argv (PLAYBOOK §11, App. C).

The prompt is *not* in argv: it is fed on stdin (no argv size limit, not visible in ``ps``).

Permission mapping (verified against ``claude --help`` 2.1.283; live behavior is checked in M10):
* non-interactive: ``dontAsk`` denies anything not explicitly allowed; ``acceptEdits`` when no
  restriction was requested.
* read-only -> only Read/Glob/Grep, and Edit/Write/NotebookEdit/Bash denied.
* write scope -> native path rules ``Edit(<glob>)`` / ``Write(<glob>)`` (best effort; the control
  plane re-checks scope after execution, §10.4).
* network ``deny`` -> WebFetch/WebSearch denied.
"""

from __future__ import annotations

from aix.agents.protocol import AgentRequest

READ_TOOLS = ["Read", "Glob", "Grep"]
WRITE_TOOLS = ["Edit", "Write", "NotebookEdit", "Bash"]
WEB_TOOLS = ["WebFetch", "WebSearch"]


def build_argv(binary: str, req: AgentRequest) -> list[str]:
    """Full command line for one attempt."""
    perms = req.permissions
    argv = [binary, "-p", "--output-format", "stream-json", "--verbose"]
    if req.model:
        argv += ["--model", req.model]
    if req.session_ref:
        argv += ["--resume", req.session_ref]

    allowed: list[str] = []
    denied: list[str] = []
    if perms.read_only:
        mode = "dontAsk"
        allowed = list(perms.allowed_tools) if perms.allowed_tools is not None else list(READ_TOOLS)
        denied = list(WRITE_TOOLS)
    elif perms.write_scope:
        mode = "dontAsk"
        allowed = list(READ_TOOLS)
        for glob in perms.write_scope:
            allowed += [f"Edit({glob})", f"Write({glob})"]
        allowed += perms.allowed_tools or []
    else:
        mode = "acceptEdits"
        allowed = list(perms.allowed_tools or [])
    if perms.network == "deny":
        denied += WEB_TOOLS

    argv += ["--permission-mode", mode]
    if allowed:
        argv += ["--allowedTools", ",".join(allowed)]
    if denied:
        argv += ["--disallowedTools", ",".join(denied)]
    return argv
