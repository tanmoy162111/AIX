"""The sample adapter: a scripted in-process agent, packaged like a real adapter plugin.

An adapter plugin's entrypoint is a ``create() -> AgentAdapter`` factory, and its package ships a
``manifest.yaml`` (the :class:`aix.agents.manifest.AdapterManifest`) next to it, exactly as the
built-in adapters do.
"""

from __future__ import annotations

from pathlib import Path

from aix.agents.adapters.fake import FakeAdapter
from aix.agents.adapters.fake.script import load_scripts
from aix.agents.protocol import AgentAdapter
from aix.domain.enums import Capability

HERE = Path(__file__).resolve().parent


def create() -> AgentAdapter:
    scripts = HERE.parent / "scripts"
    return FakeAdapter(
        "sample-agent",
        name="Sample plugin agent",
        scripts=load_scripts(scripts),
        base_dir=scripts,
        capabilities={cap: 0.9 for cap in Capability},
    )
