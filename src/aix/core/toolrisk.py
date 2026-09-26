"""Classify control-plane commands into risk classes for the ``tool_risk`` point (§18.2, A.3).

Only the executable and a few sub-command tokens are looked at, never file contents or agent
text. The result feeds ``DecisionState.tool.classes`` (control-plane labels only).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Final

_READ_ONLY_EXE: Final = frozenset(
    {"cat", "ls", "grep", "head", "tail", "wc", "diff", "find", "echo"}
)
_NETWORK_EXE: Final = frozenset({"curl", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet"})
_DEPLOY_WORDS: Final = frozenset({"deploy", "publish", "release"})
_DEPLOY_PAIRS: Final = {
    "kubectl": {"apply", "delete", "rollout", "replace"},
    "terraform": {"apply", "destroy"},
    "helm": {"install", "upgrade", "uninstall"},
    "docker": {"push"},
    "gcloud": {"deploy"},
    "vercel": {"deploy", "--prod"},
}
_MIGRATE: Final = {
    "alembic": {"upgrade", "downgrade"},
    "prisma": {"migrate"},
    "flask": {"db"},
    "rails": {"db:migrate"},
    "rake": {"db:migrate"},
}
_GIT_READ: Final = frozenset({"status", "diff", "log", "show", "rev-parse", "branch", "ls-files"})
_GIT_DELETE: Final = frozenset({"clean", "gc"})


SAFE_CLASSES: Final = frozenset({"read_only", "local_write"})
"""Classes that never need a decision (ordinary build/test/lint commands)."""
ACTION_OF: Final = {"git_push": "push"}
"""Risk class -> the ``security.approval_required_for`` action name, where they differ."""


def gated_actions(classes: Sequence[str]) -> list[str]:
    """Action names to check against ``approval_required_for`` for these classes."""
    return [ACTION_OF.get(c, c) for c in classes]


def _classify_one(argv: Sequence[str]) -> set[str]:
    if not argv:
        return {"unknown"}
    exe = Path(argv[0]).name
    rest = list(argv[1:])
    tokens = set(rest)
    out: set[str] = set()
    if exe == "git":
        sub = next((a for a in rest if not a.startswith("-")), "")
        if sub == "push":
            out.add("git_push")
        elif sub in _GIT_DELETE or (sub == "reset" and "--hard" in tokens):
            out.add("local_delete")
        elif sub in _GIT_READ:
            out.add("read_only")
        else:
            out.add("local_write")
        return out
    if exe == "rm":
        return {"local_delete"}
    if exe in _NETWORK_EXE:
        return {"external_network"}
    if exe in _READ_ONLY_EXE:
        return {"read_only"}
    if tokens & _DEPLOY_WORDS or (exe in _DEPLOY_PAIRS and tokens & _DEPLOY_PAIRS[exe]):
        out.add("deploy")
    if exe in _MIGRATE and tokens & _MIGRATE[exe]:
        out.add("db_migration_apply")
    if exe == "python" and "manage.py" in tokens and "migrate" in tokens:
        out.add("db_migration_apply")
    if out:
        return out
    if exe in {"python", "python3", "pytest", "ruff", "mypy", "pyright", "npm", "npx", "pnpm",
               "yarn", "node", "go", "cargo", "make", "uv", "tsc", "sh", "bash"}:  # fmt: skip
        return {"local_write"}
    return {"unknown"}


def classify_command(argv: Sequence[str]) -> list[str]:
    """Risk classes of a command (sorted, unique). ``sh -c`` strings are split on ``&&``/``;``."""
    if len(argv) >= 3 and Path(argv[0]).name in {"sh", "bash"} and argv[1] == "-c":
        classes: set[str] = set()
        for part in argv[2].replace("&&", ";").replace("||", ";").split(";"):
            words = part.split()
            if words:
                classes |= _classify_one(words)
        return sorted(classes or {"unknown"})
    return sorted(_classify_one(argv))
