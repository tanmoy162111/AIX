"""Capture a fresh raw stream from a real agent CLI (PLAYBOOK §10.5, M10.1)."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import anyio
import anyio.to_thread

from aix.agents.protocol import AgentAdapter, AgentOutcome, AgentPermissions, AgentRequest
from aix.domain.ids import IdPrefix, new_id

RECORD_PROMPT = "Reply with the single word OK. Do not use any tools."
RECORD_FILE = "live_read_only.jsonl"


def _scratch_repo(root: Path) -> Path:
    """A throwaway git repo so the agent never sees the user's tree."""
    root.mkdir(parents=True)
    (root / "README.md").write_text("scratch\n")
    env = {
        "GIT_AUTHOR_NAME": "aix",
        "GIT_AUTHOR_EMAIL": "aix@example.invalid",
        "GIT_COMMITTER_NAME": "aix",
        "GIT_COMMITTER_EMAIL": "aix@example.invalid",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "scratch"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)
    return root


_DROP_SUBTYPES = frozenset({"hook_started", "hook_response", "commands_changed"})
_NOISY_KEYS = frozenset(
    {
        "tools", "mcp_servers", "slash_commands", "terminal_slash_commands", "skills", "plugins",
        "agents", "commands", "capabilities", "memory_paths", "messaging_socket_path",
    }
)  # fmt: skip


def sanitize_recording(path: Path, home: Path | None = None) -> None:
    """Strip machine-specific noise from a recording so it is safe to commit.

    Drops hook/command-list system events, blanks the user's tool/plugin/skill inventories and
    replaces the home directory with ``~``. Lines that are not JSON objects are kept verbatim
    (apart from the home rewrite) because malformed input is a parser concern.
    """
    home_str = str(home or Path.home())
    out: list[str] = []
    for line in path.read_text().splitlines():
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
    path.write_text("\n".join(out) + "\n")


async def record_read_only(adapter: AgentAdapter, out_dir: Path) -> tuple[Path, AgentOutcome]:
    """Run one tiny read-only prompt and mirror its sanitized stdout to ``out_dir``.

    The recording is ``out_dir/live_read_only.jsonl``.

    Returns the recording path and the outcome. The recording is written even when the run
    fails, because failure streams are exactly what the classifier tests need; the caller decides
    what to keep. The agent runs in a scratch repo under the OS temp dir.
    """
    await anyio.to_thread.run_sync(lambda: out_dir.mkdir(parents=True, exist_ok=True))
    target = out_dir / RECORD_FILE
    with tempfile.TemporaryDirectory(prefix="aix-record-") as tmp:
        request = AgentRequest(
            attempt_id=new_id(IdPrefix.ATTEMPT),
            workspace=await anyio.to_thread.run_sync(_scratch_repo, Path(tmp) / "ws"),
            prompt=RECORD_PROMPT,
            timeout_s=180,
            permissions=AgentPermissions(read_only=True),
            stream_path=target,
        )
        handle = await adapter.start(request)
        async for _ in adapter.events(handle):
            pass
        outcome = await adapter.wait(handle)
    await anyio.to_thread.run_sync(sanitize_recording, target)
    return target, outcome
