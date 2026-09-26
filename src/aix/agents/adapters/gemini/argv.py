"""Translate an ``AgentRequest`` into a headless ``gemini`` command line (PLAYBOOK §11, App. C).

Flags verified against ``gemini --help`` (gemini-cli 0.55.1). ``-p`` puts the CLI in headless mode
and its value is *appended to stdin*, so the task prompt itself goes on stdin and ``-p`` carries
only a fixed, non-sensitive instruction (no argv size limit, nothing in ``ps``). ``--skip-trust`` is
required in headless mode for workspaces the CLI has not been told to trust; without it the CLI
refuses to run in an untrusted folder.

Permission mapping:
* ``read_only`` -> ``--approval-mode plan`` (the CLI's read-only mode).
* otherwise -> ``--approval-mode auto_edit`` (file edits auto-approved; shell commands are not
  approved in headless mode, so the agent cannot run tests itself; the control plane runs the
  checks, §17). ``yolo`` is never passed.
* ``network``, ``write_scope`` and ``allowed_tools`` cannot be expressed reliably by this CLI (the
  ``--allowed-tools`` flag is deprecated in favor of policy files); the control plane's post-run
  scope check is the only enforcement (§10.4).
"""

from __future__ import annotations

from aix.agents.protocol import AgentRequest

INSTRUCTION = "Follow the task given on standard input."


def build_argv(binary: str, req: AgentRequest) -> list[str]:
    """Full command line for one attempt."""
    mode = "plan" if req.permissions.read_only else "auto_edit"
    argv = [binary, "--output-format", "stream-json", "--approval-mode", mode, "--skip-trust"]
    if req.model:
        argv += ["--model", req.model]
    if req.session_ref:
        argv += ["--resume", req.session_ref]
    argv += ["--prompt", INSTRUCTION]
    return argv
