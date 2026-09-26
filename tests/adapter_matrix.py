"""Rigs that build each adapter in a given scenario for the shared §10.5 test matrix."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aix.agents.adapters.claude import ClaudeAdapter
from aix.agents.adapters.codex import CodexAdapter
from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import FakeScript, FakeStep
from aix.agents.protocol import AgentAdapter, AgentPermissions, AgentRequest
from aix.domain.ids import IdPrefix, new_id
from binaries import make_replay_binary

RECORDINGS = Path(__file__).resolve().parent / "fixtures" / "recordings"

# PLAYBOOK §10.5, one row each.
MATRIX_ROWS = (
    "probe",
    "success",
    "nonzero_exit",
    "malformed_stream_line",
    "timeout",
    "cancellation",
    "auth_failure",
    "rate_limit",
    "usage_parsing",
    "session_resume",
    "permission_translation",
)

# Rows that do not apply to an adapter, with the reason (everything else must be covered).
NOT_APPLICABLE: dict[tuple[str, str], str] = {
    ("fake", "malformed_stream_line"): "in-process agent: there is no byte stream to corrupt",
    ("fake", "permission_translation"): "in-process agent: no CLI flags; it ignores permissions",
}

HELP = {
    "claude": "--output-format --verbose --permission-mode --allowedTools --disallowedTools "
    "--model --resume",
    "codex": "--json --sandbox --model --cd --config resume",
}


@dataclass
class Case:
    adapter: AgentAdapter
    req: AgentRequest
    log_dir: Path | None = None


class Rig:
    """Builds ``Case``s for one adapter. ``kind`` is ``fake``, ``claude`` or ``codex``."""

    def __init__(self, kind: str, tmp: Path) -> None:
        self.kind, self.tmp = kind, tmp
        self.bin, self.log, self.ws = tmp / "bin", tmp / "log", tmp / "ws"
        self.ws.mkdir()

    # ---- request -----------------------------------------------------------------------
    def req(self, **kw: Any) -> AgentRequest:
        data: dict[str, Any] = {
            "attempt_id": new_id(IdPrefix.ATTEMPT),
            "workspace": self.ws,
            "prompt": "TASK TYPE: implement\ndo it",
            "timeout_s": 30,
            "permissions": AgentPermissions(),
        }
        data.update(kw)
        return AgentRequest.model_validate(data)

    # ---- adapters ----------------------------------------------------------------------
    def _cli(self, recording: str | None, **kw: Any) -> AgentAdapter:
        make_replay_binary(
            self.bin,
            self.kind,
            recording=RECORDINGS / self.kind / recording if recording else None,
            log_dir=self.log,
            help_text=HELP[self.kind],
            **kw,
        )
        env = {"PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "HOME": str(self.tmp)}
        return (
            ClaudeAdapter(env_source=env) if self.kind == "claude" else CodexAdapter(env_source=env)
        )

    def _fake(self, **step: Any) -> AgentAdapter:
        return FakeAdapter("fake", scripts=[FakeScript(attempts=[FakeStep.model_validate(step)])])

    def build(self, scenario: str) -> Case:
        fake = self.kind == "fake"
        no_result = "no_result.jsonl" if self.kind == "claude" else "no_completion.jsonl"
        req = self.req()
        match scenario:
            case "success":
                a = self._fake(claim="ok") if fake else self._cli("success.jsonl")
            case "nonzero_exit":
                a = (
                    self._fake(exit_code=2, stderr="crash")
                    if fake
                    else self._cli(None, exit_code=2, stderr="crash\n")
                )
            case "malformed_stream_line":
                a = self._cli("malformed.jsonl")
            case "timeout":
                a = self._fake(sleep_s=5) if fake else self._cli(no_result, sleep_s=30)
                req = self.req(timeout_s=1)
            case "cancellation":
                a = self._fake(sleep_s=30) if fake else self._cli(no_result, sleep_s=30)
            case "auth_failure":
                a = (
                    self._fake(exit_code=1, stderr="401 Unauthorized")
                    if fake
                    else self._cli("auth_failure.jsonl", exit_code=1)
                )
            case "rate_limit":
                a = (
                    self._fake(exit_code=1, stderr="429 rate limit")
                    if fake
                    else self._cli("rate_limit.jsonl", exit_code=1)
                )
            case "usage_parsing":
                a = (
                    self._fake(usage={"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.01})
                    if fake
                    else self._cli("success.jsonl")
                )
            case "session_resume":
                a = self._fake() if fake else self._cli("success.jsonl")
            case _:
                raise KeyError(scenario)
        return Case(a, req, self.log)

    def argv(self) -> list[str]:
        return json.loads((self.log / "argv.json").read_text())

    # ---- probe ---------------------------------------------------------------------------
    def probe_ready(self) -> AgentAdapter:
        if self.kind == "fake":
            return FakeAdapter("fake")
        return self._cli(None, version="1.2.3")

    def probe_missing(self) -> AgentAdapter:
        if self.kind == "fake":
            return FakeAdapter("fake", health="unavailable", health_reason="scripted outage")
        env = {"PATH": str(self.tmp / "nothing-here")}
        return (
            ClaudeAdapter(env_source=env) if self.kind == "claude" else CodexAdapter(env_source=env)
        )
