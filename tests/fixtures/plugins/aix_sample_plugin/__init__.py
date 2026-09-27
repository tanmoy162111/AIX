"""Sample external aix plugin (PLAYBOOK §25): an adapter, a check and two deliberately broken ones.

Installed for tests by ``tests/plugin_env.py`` as a distribution with these entry points::

    [aix.adapters]
    sample-agent = aix_sample_plugin:ADAPTER
    future-agent = aix_sample_plugin:FUTURE_ADAPTER      # requires a newer aix: skipped, reported
    ghost-agent  = aix_sample_plugin:GHOST_ADAPTER       # entrypoint does not import: reported
    [aix.checks]
    no-todo = aix_sample_plugin:CHECK

Each entry point resolves to a plain manifest mapping; the code behind ``entrypoint`` is imported
only when the plugin is activated.
"""

from __future__ import annotations

ADAPTER = {
    "id": "sample-agent",
    "version": "0.1.0",
    "type": "adapter",
    "capabilities": ["implement", "test", "document"],
    "permissions": ["spawn_process"],
    "requires_aix": ">=0.1",
    "entrypoint": "aix_sample_plugin.adapter:create",
}
FUTURE_ADAPTER = {
    **ADAPTER,
    "id": "future-agent",
    "requires_aix": ">=99.0",
}
GHOST_ADAPTER = {
    **ADAPTER,
    "id": "ghost-agent",
    "entrypoint": "aix_sample_plugin.does_not_exist:create",
}
CHECK = {
    "id": "no-todo",
    "version": "0.1.0",
    "type": "check",
    "capabilities": ["custom"],
    "permissions": ["read_workspace"],
    "requires_aix": ">=0.1,<1",
    "entrypoint": "aix_sample_plugin.checks:no_todo",
}
