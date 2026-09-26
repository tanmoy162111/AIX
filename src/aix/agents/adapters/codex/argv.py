"""Translate an ``AgentRequest`` into ``codex exec`` argv (PLAYBOOK §11, App. C).

Flags verified against ``codex exec --help`` (codex-cli 0.147.0). The prompt is read from stdin
(``-``). Non-interactive: ``-c approval_policy="never"`` (approval is a top-level ``codex`` flag,
not an ``exec`` flag, so it is set through a config override).

Permission mapping:
* ``read_only`` -> ``-s read-only``; otherwise ``-s workspace-write``.
* ``network: allow`` -> ``-c sandbox_workspace_write.network_access=true`` (denied otherwise).
* ``write_scope`` and ``allowed_tools`` cannot be expressed by this CLI; the control plane's
  post-run scope check is the only enforcement (§10.4), so the policy engine treats codex as
  unable to enforce write scope.
"""

from __future__ import annotations

from aix.agents.protocol import AgentRequest


def build_argv(binary: str, req: AgentRequest) -> list[str]:
    """Full command line for one attempt."""
    perms = req.permissions
    sandbox = "read-only" if perms.read_only else "workspace-write"
    argv = [
        binary,
        "exec",
        "--json",
        "-C",
        str(req.workspace),
        "-s",
        sandbox,
        "-c",
        'approval_policy="never"',
    ]
    if req.model:
        argv += ["-m", req.model]
    if perms.network == "allow" and not perms.read_only:
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    if req.session_ref:
        argv += ["resume", req.session_ref]
    argv.append("-")
    return argv
