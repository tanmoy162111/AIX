"""Helpers for CLI tests that need a hermetic PATH with only git and chosen fake agents."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from binaries import make_replay_binary

RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "recordings"
HELP = {
    "claude": "--output-format --verbose --permission-mode --allowedTools --disallowedTools "
    "--model --resume",
    "codex": "--json --sandbox --model --cd --config resume",
    "gemini": "--prompt --output-format --approval-mode --model --skip-trust --resume",
    "opencode": "run --format --model --session --dir --agent",
}


def install(env: Path, kind: str, recording: str | None = None, **kw: object) -> None:
    """Put a fake ``kind`` CLI on the hermetic PATH (logs invocations to ``<env>/log-<kind>``)."""
    make_replay_binary(
        env / "bin",
        kind,
        recording=RECORDINGS / kind / recording if recording else None,
        help_text=HELP[kind],
        version=f"{kind} 1.2.3",
        log_dir=env / f"log-{kind}",
        **kw,  # type: ignore[arg-type]
    )


def hermetic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PATH holds only git, so no real agent CLI on the machine can be probed or run."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    git = shutil.which("git")
    assert git
    if not (bin_dir / "git").exists():
        (bin_dir / "git").symlink_to(git)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
