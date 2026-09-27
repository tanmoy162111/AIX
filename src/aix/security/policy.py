"""Policy engine (PLAYBOOK §20.2): what agents, commands and writes are allowed.

Declarative and versioned: :attr:`Policy.hash` covers the policy code version and the effective
security configuration, so a decision or artifact can name the exact policy it was made under.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Final

from aix.agents.protocol import AgentPermissions
from aix.config.schema import SecurityConfig
from aix.domain.agents import AgentSpec
from aix.domain.tasks import Task

POLICY_VERSION: Final = "policy-v2"
_PROTECTED_ROOTS: Final = (".aix", ".git")
_NETWORK_VALUES: Final = ("deny", "provider_default", "allow")
_FORBIDDEN_CALLS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    (
        "approval_bypass",
        re.compile(r"\baix\b.*\b(approve|deny|approvals)\b|aix\.cli\.main.*\b(approve|deny)\b"),
    ),
    ("secret_path", re.compile(r"approval_token|\.config/aix|/aix/approval")),
    ("control_plane_state", re.compile(r"(^|[\s/'\"=])\.aix(/|\b)")),
    (
        "irreversible_action",
        re.compile(r"\bgit\s+(push|config|remote)\b|--force-with-lease|\brm\s+-rf\s+/"),
    ),
)

_STATE_TARGET: Final = r"""['"]?\.?/?\.aix(?:/[^\s'"]*)?['"]?"""
_STATE_EXCLUSIONS: Final = re.compile(
    rf"""(?:-path\s+{_STATE_TARGET}\s+-prune"""  # find ... -path ./.aix -prune
    rf"""|(?:-not|!)\s+-(?:i?path|i?name)\s+{_STATE_TARGET}"""  # find ... -not -path './.aix/*'
    rf"""|--exclude(?:-dir)?[=\s]+{_STATE_TARGET}"""  # grep --exclude-dir=.aix
    rf"""|-g\s+['"]?!\.aix[^\s'"]*['"]?)"""  # rg -g '!.aix'
)
"""Ways of *skipping* the state directory. Mentioning it to exclude it does not touch it."""


@dataclass(frozen=True)
class Verdict:
    """A policy answer that can always explain itself."""

    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.allowed


class Policy:
    """Policy derived from :class:`SecurityConfig`. Immutable; cheap to build."""

    def __init__(self, security: SecurityConfig) -> None:
        self._sec = security

    @property
    def sandbox(self) -> str:
        return self._sec.sandbox

    @property
    def hash(self) -> str:
        """SHA-256 over the policy version and the effective security configuration."""
        doc = {"version": POLICY_VERSION, "security": self._sec.model_dump(mode="json")}
        blob = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ---- agents -------------------------------------------------------------------------

    def required_restrictions(self, task: Task) -> list[str]:
        """Restrictions the task depends on: ``read_only`` or a narrowed ``write_scope``."""
        if not task.file_scope:
            return ["read_only"]
        if task.file_scope != ["**"]:
            return ["write_scope"]
        return []

    def can_run_agent(self, agent: AgentSpec, task: Task) -> Verdict:
        """Whether ``agent`` may take ``task``.

        A ``high``-risk task in ``local`` sandbox mode needs an agent whose own CLI enforces every
        restriction the task depends on (§20.2); otherwise only the post-run scope check would
        stand between the agent and the rest of the tree. In ``container`` mode the container
        confines the filesystem, so the native-enforcement rule is not applied.
        """
        if task.risk != "high" or self._sec.sandbox == "container":
            return Verdict(True)
        missing = [r for r in self.required_restrictions(task) if r not in agent.supports.enforces]
        if missing:
            return Verdict(
                False, f"{agent.id} cannot enforce {', '.join(missing)} for a high-risk task"
            )
        return Verdict(True)

    def agent_permissions(self, task: Task) -> AgentPermissions:
        """What an agent may do for ``task``, before each adapter maps it to native flags."""
        network = self._sec.network.agents
        return AgentPermissions(
            read_only=not task.file_scope,
            write_scope=[] if task.file_scope == ["**"] else list(task.file_scope),
            network=network if network in _NETWORK_VALUES else "provider_default",  # type: ignore[arg-type]
        )

    # ---- commands, approvals and writes -----------------------------------------------------

    def can_exec(self, argv: list[str]) -> Verdict:
        """Whether the control plane may run ``argv`` (allowlisted executable, no path tricks)."""
        if not argv:
            return Verdict(False, "empty command")
        exe = PurePosixPath(argv[0]).name
        if exe not in self._sec.shell_allow:
            return Verdict(False, f"{exe} is not in security.shell_allow")
        return Verdict(True)

    def inspect_tool_call(
        self, name: str, tool_input: str, workspace: str | None = None
    ) -> tuple[str, str] | None:
        """``(kind, detail)`` when an agent tool call touches something agents must never touch.

        Covers granting approvals, the approval token, the aix state directory and irreversible
        git actions. This is *detection after the fact* from the agent's own stream: it records
        the attempt and fails it, but it cannot stop a same-user process in ``local`` mode
        (see docs/security.md).

        ``workspace`` is the attempt's own worktree. It lives under ``.aix/worktrees/`` and agents
        report absolute paths, so that prefix is removed before matching; a path *below* it is
        judged as a repo-relative path. Commands that only *skip* the state directory
        (``find -path ./.aix -prune``, ``--exclude-dir=.aix``) are not touching it and pass.
        """
        text = f"{name} {tool_input}"
        if workspace:
            text = text.replace(workspace.rstrip("/"), ".")
        text = _STATE_EXCLUSIONS.sub(" ", text)
        for kind, pattern in _FORBIDDEN_CALLS:
            if pattern.search(text):
                return kind, f"{name}: {tool_input[:200]}"
        return None

    def requires_approval(self, action: str) -> bool:
        return action in self._sec.approval_required_for

    def can_write(self, path: str, task: Task) -> Verdict:
        """Whether a change to ``path`` (repo-relative) is inside ``task``'s scope."""
        from aix.core.workspace.scope import scope_violations

        parts = PurePosixPath(path).parts
        if path.startswith("/") or ".." in parts:
            return Verdict(False, "path escapes the workspace")
        if parts and parts[0] in _PROTECTED_ROOTS:
            return Verdict(False, f"{parts[0]}/ is protected")
        if scope_violations([path], task.file_scope, read_only=not task.file_scope):
            return Verdict(False, "outside the task's file scope")
        return Verdict(True)
