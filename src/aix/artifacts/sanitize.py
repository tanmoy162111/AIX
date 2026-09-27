"""Remove machine-specific noise from raw agent streams (PLAYBOOK §22, ADR-0038).

Agent CLIs echo their environment: claude's ``system/init`` lists every installed tool, skill,
plugin (with absolute paths) and MCP server, and it also emits hook output. That is neither
evidence about the run nor safe to share, so it is stripped from streams that are published as
artifacts and recordings. The raw stream stays in ``.aix/runs/`` on the machine that ran it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

_DROP_SUBTYPES: Final = frozenset({"hook_started", "hook_response", "commands_changed"})
_NOISY_KEYS: Final = frozenset(
    {
        "tools", "mcp_servers", "slash_commands", "terminal_slash_commands", "skills", "plugins",
        "agents", "commands", "capabilities", "memory_paths", "messaging_socket_path",
    }
)  # fmt: skip


def sanitize_stream(text: str, home: Path | None = None) -> str:
    """Return ``text`` (JSON lines) without hook/command-list events and inventories.

    Contract: drops ``system`` events of the hook/command-list subtypes, empties the inventory
    keys of every other JSON object, and rewrites the home directory to ``~`` everywhere.
    Lines that are not JSON objects are kept (with the home rewrite) because malformed input is
    a parser concern. Pure and idempotent; the result ends with a newline unless empty.
    """
    home_str = str(home or Path.home())
    out: list[str] = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            out.append(line.replace(home_str, "~"))
            continue
        if not isinstance(obj, dict):
            out.append(line.replace(home_str, "~"))
            continue
        if obj.get("type") == "system" and obj.get("subtype") in _DROP_SUBTYPES:
            continue
        for key in _NOISY_KEYS & obj.keys():
            obj[key] = []
        out.append(json.dumps(obj, separators=(",", ":")).replace(home_str, "~"))
    return "\n".join(out) + "\n" if out else ""


def sanitize_stream_bytes(data: bytes, home: Path | None = None) -> bytes:
    """:func:`sanitize_stream` on UTF-8 bytes (undecodable bytes are replaced)."""
    return sanitize_stream(data.decode("utf-8", errors="replace"), home).encode("utf-8")
