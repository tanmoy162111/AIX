"""Agent process environment: a minimal base plus an explicit allowlist (PLAYBOOK §20.5).

Everything else in the parent environment is dropped, so provider credentials only reach the
adapter they belong to. Request-supplied variables (already policy-filtered) are added last.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

BASE_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TERM")


def build_agent_env(
    allowlist: Iterable[str],
    extra: Mapping[str, str],
    *,
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Base keys + allowlisted keys from ``source`` (default ``os.environ``) + ``extra``."""
    src = os.environ if source is None else source
    env = {k: src[k] for k in (*BASE_ENV_KEYS, *allowlist) if k in src}
    env.update(extra)
    env["AIX_AGENT_CONTEXT"] = "1"  # marks the process as an agent; `aix approve` refuses (§20.4)
    return env
