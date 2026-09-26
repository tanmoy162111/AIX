from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from aix.agents.manifest import AdapterManifest
from aix.domain.enums import Capability

SPEC_EXAMPLE = """
id: codex
name: Codex CLI
kind: cli
binary: codex
probe: { version_args: ["--version"], auth_check: "exec_smoke" }
capabilities: { implement: 0.85, debug: 0.8, test: 0.7, review: 0.6, design: 0.5, research: 0.3 }
cost_class: medium
supports:
  {streaming: true, sessions: true, non_interactive: true, cancel: true, cost_reporting: partial}
"""


def test_spec_example_parses() -> None:
    m = AdapterManifest.model_validate(yaml.safe_load(SPEC_EXAMPLE))
    assert m.id == "codex" and m.binary == "codex"
    assert m.probe.version_args == ["--version"] and m.probe.auth_check == "exec_smoke"
    assert m.capabilities[Capability.IMPLEMENT] == 0.85
    assert m.supports.cost_reporting == "partial"
    assert m.env_allowlist == [] and m.required_flags == []


def test_binary_is_optional_for_in_process_agents() -> None:
    m = AdapterManifest(id="fake", name="Fake", kind="local", cost_class="free")
    assert m.binary is None


def test_validation() -> None:
    base = yaml.safe_load(SPEC_EXAMPLE)
    with pytest.raises(ValidationError):
        AdapterManifest.model_validate({**base, "capabilities": {"implement": 2}})
    with pytest.raises(ValidationError):
        AdapterManifest.model_validate({**base, "surprise": 1})
    with pytest.raises(ValidationError):
        AdapterManifest.model_validate({**base, "id": "Bad Id"})


def test_env_allowlist_rejects_lowercase_or_odd_names() -> None:
    base = yaml.safe_load(SPEC_EXAMPLE)
    assert AdapterManifest.model_validate({**base, "env_allowlist": ["OPENAI_API_KEY"]})
    with pytest.raises(ValidationError):
        AdapterManifest.model_validate({**base, "env_allowlist": ["bad-name"]})
