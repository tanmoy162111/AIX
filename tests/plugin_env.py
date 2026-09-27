"""Install the sample external plugin (``tests/fixtures/plugins``) as a real distribution.

Entry points are found by ``importlib.metadata`` scanning ``sys.path`` for ``*.dist-info``, so the
helper builds a throw-away "site" directory holding the package and its metadata, and puts it on
``sys.path`` for the duration of a test.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

import pytest

PLUGINS = Path(__file__).resolve().parent / "fixtures" / "plugins"
SAMPLE = "aix_sample_plugin"
DIST = "aix-sample-plugin"
ENTRY_POINTS = """\
[aix.adapters]
sample-agent = aix_sample_plugin:ADAPTER
future-agent = aix_sample_plugin:FUTURE_ADAPTER
ghost-agent = aix_sample_plugin:GHOST_ADAPTER

[aix.checks]
no-todo = aix_sample_plugin:CHECK
"""


def install_sample_plugin(site: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Copy the sample package + dist-info into ``site`` and make it importable and discoverable."""
    site.mkdir(parents=True, exist_ok=True)
    shutil.copytree(PLUGINS / SAMPLE, site / SAMPLE, ignore=shutil.ignore_patterns("__pycache__"))
    info = site / "aix_sample_plugin-0.1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {DIST}\nVersion: 0.1.0\n")
    (info / "entry_points.txt").write_text(ENTRY_POINTS)
    monkeypatch.syspath_prepend(str(site))
    importlib.invalidate_caches()
    for name in [m for m in sys.modules if m == SAMPLE or m.startswith(SAMPLE + ".")]:
        monkeypatch.delitem(sys.modules, name)
    return site
