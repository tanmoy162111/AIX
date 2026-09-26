"""Approval integrity (PLAYBOOK §20.4): TTY confirmation, token file, agent-context guard."""

from __future__ import annotations

import getpass
import hmac
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path

AGENT_CONTEXT_ENV = "AIX_AGENT_CONTEXT"
SHORT_ID_LEN = 6


class ApprovalRefused(Exception):
    """An approval attempt was refused (agent context, bad token, failed confirmation)."""


def config_dir() -> Path:
    """``$XDG_CONFIG_HOME/aix`` (default ``~/.config/aix``)."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "aix"


def token_path() -> Path:
    return config_dir() / "approval_token"


def ensure_secret(path: Path) -> str:
    """Return the secret stored at ``path``, creating it (mode 0600) on first use."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_urlsafe(32) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return path.read_text(encoding="utf-8").strip()


def ensure_token() -> str:
    """Return the approval token, creating it (mode 0600) on first use.

    The token lives only in the user config dir; it is never placed in an agent environment,
    context or artifact.
    """
    return ensure_secret(token_path())


def guard_agent_context(env: dict[str, str] | None = None) -> None:
    """Refuse when running inside an agent subprocess (``AIX_AGENT_CONTEXT`` set).

    Raises:
        ApprovalRefused: the variable is present and non-empty.
    """
    if (env if env is not None else os.environ).get(AGENT_CONTEXT_ENV):
        raise ApprovalRefused("approvals cannot be granted from inside an agent context")


def short_id(approval_id: str) -> str:
    """The last characters of an approval id, typed back to confirm on a TTY."""
    return approval_id[-SHORT_ID_LEN:].lower()


def confirm_tty(
    approval_id: str,
    *,
    isatty: bool,
    prompt: Callable[[str], str] = input,
) -> None:
    """Require a human at a terminal to type the short id back.

    Raises:
        ApprovalRefused: not a TTY, or the typed text does not match.
    """
    if not isatty:
        raise ApprovalRefused("not an interactive terminal; use --token")
    typed = prompt(f"Type '{short_id(approval_id)}' to confirm: ").strip().lower()
    if not hmac.compare_digest(typed, short_id(approval_id)):
        raise ApprovalRefused("confirmation did not match")


def verify_token(given: str | None) -> None:
    """Non-TTY approval: the token must match the stored one (constant-time compare).

    Raises:
        ApprovalRefused: no token given, or it does not match.
    """
    if not given:
        raise ApprovalRefused("a non-interactive approval requires --token")
    if not hmac.compare_digest(given, ensure_token()):
        raise ApprovalRefused("invalid approval token")


def actor() -> str:
    """The OS user recorded as the approver."""
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"
