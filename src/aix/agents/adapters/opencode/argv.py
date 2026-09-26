"""Translate an ``AgentRequest`` into an ``opencode run`` command line (PLAYBOOK §11, App. C).

Flags verified against ``opencode run --help`` (opencode 1.18.26) and the CLI's own handler: the
message arguments are joined with any piped stdin, and an empty combination is rejected, so the
prompt goes on **stdin** and no positional message is passed (no argv size limit, nothing in
``ps``). ``--dir`` makes the target directory explicit.

Permission mapping:
* ``read_only`` -> ``--agent plan`` (the built-in primary agent that denies edits).
* otherwise the default ``build`` agent. Permission prompts that the config leaves at ``ask``
  (for example paths outside the workspace) are **auto-rejected** in ``run`` mode unless
  ``--auto`` is given, and ``--auto`` (and its hidden aliases ``--yolo`` and
  ``--dangerously-skip-permissions``) is never passed.
* ``network``, ``write_scope`` and ``allowed_tools`` cannot be expressed by this CLI; only the
  control plane's post-run scope check (§10.4) applies.
"""

from __future__ import annotations

from aix.agents.protocol import AgentRequest


def build_argv(binary: str, req: AgentRequest) -> list[str]:
    """Full command line for one attempt."""
    argv = [binary, "run", "--format", "json", "--dir", str(req.workspace)]
    if req.permissions.read_only:
        argv += ["--agent", "plan"]
    if req.model:
        argv += ["--model", req.model]
    if req.session_ref:
        argv += ["--session", req.session_ref]
    return argv
