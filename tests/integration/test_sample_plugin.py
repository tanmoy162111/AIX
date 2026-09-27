"""The sample external plugin (adapter + check) loaded and used end to end (PLAYBOOK §25, M9.4)."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import repos
from aix.agents.registry import AdapterRegistry
from aix.config.schema import AgentsConfig, AixConfig, PluginCheckRef, VerificationConfig
from aix.core.orchestrator.executor import RunRequest, execute_run
from aix.domain.enums import CheckKind as K
from aix.domain.enums import RunStatus
from aix.domain.tasks import VerificationSpec
from aix.store.db import EventStore
from aix.verification.engine import run_verification
from aix.verification.plugins import load_plugin_checks
from plugin_env import install_sample_plugin
from verif_env import fast_config

pytestmark = pytest.mark.anyio

CLEAN = "+++ b/a.py\n+x = 1\n"
DIRTY = "+++ b/a.py\n+x = 1  # TODO later\n"


@pytest.fixture(autouse=True)
def sample(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install_sample_plugin(tmp_path / "site", monkeypatch)


def cfg(*refs: PluginCheckRef) -> AixConfig:
    return AixConfig(verification=VerificationConfig(plugin_checks=list(refs)))


async def verify(config: AixConfig, patch: str, tmp_path: Path):  # type: ignore[no-untyped-def]
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return await run_verification(
        ws,
        "att_01HZZZZZZZZZZZZZZZZZZZZZZZ",
        VerificationSpec(required=[]),
        config,
        patch=patch,
        changed_paths=["a.py"],
        file_scope=["**"],
        plugin_checks=load_plugin_checks(config),
    )


def custom(report):  # type: ignore[no-untyped-def]
    return [c for c in report.checks if c.kind is K.CUSTOM]


async def test_a_configured_check_plugin_runs_as_a_custom_check(tmp_path: Path) -> None:
    config = cfg(PluginCheckRef(id="no-todo", required=True))
    ok = await verify(config, CLEAN, tmp_path)
    (check,) = custom(ok)
    assert check.status == "passed" and check.required and check.summary.startswith("[no-todo]")
    assert check.metrics == {"todos": 0} and ok.overall == "passed"

    bad = await verify(config, DIRTY, tmp_path)
    (check,) = custom(bad)
    assert check.status == "failed" and check.severity == "medium"
    assert bad.overall == "failed"  # required -> blocks


async def test_an_optional_plugin_failure_only_warns(tmp_path: Path) -> None:
    report = await verify(cfg(PluginCheckRef(id="no-todo")), DIRTY, tmp_path)
    assert custom(report)[0].status == "failed" and report.overall == "warning"


async def test_unconfigured_plugins_do_not_run(tmp_path: Path) -> None:
    assert custom(await verify(cfg(), DIRTY, tmp_path)) == []


async def test_a_missing_or_broken_check_plugin_is_an_error_not_a_pass(tmp_path: Path) -> None:
    config = cfg(PluginCheckRef(id="not-installed", required=True))
    report = await verify(config, CLEAN, tmp_path)
    (check,) = custom(report)
    assert check.status == "error" and "not installed" in check.summary
    assert report.overall == "failed"


async def test_a_plugin_that_raises_cannot_crash_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aix_sample_plugin.checks as mod  # type: ignore[import-not-found]

    async def boom(ctx: object) -> object:
        raise RuntimeError("kaput")

    monkeypatch.setattr(mod, "no_todo", boom)
    report = await verify(cfg(PluginCheckRef(id="no-todo", required=True)), CLEAN, tmp_path)
    (check,) = custom(report)
    assert check.status == "error" and "kaput" in check.summary


async def test_a_run_uses_the_plugin_adapter_and_the_plugin_check(tmp_path: Path) -> None:
    repo = repos.materialize_sample_py(tmp_path / "proj")
    (repo / ".aix").mkdir()
    base = fast_config()
    config = base.model_copy(
        update={
            "agents": AgentsConfig(enabled=["sample-agent"]),
            "verification": base.verification.model_copy(
                update={"plugin_checks": [PluginCheckRef(id="no-todo", required=True)]}
            ),
        }
    )
    registry = AdapterRegistry(config, builtin_ids=(), discover_plugins=True)
    assert registry.ids() == ["sample-agent"]  # future-agent / ghost-agent were skipped
    store = await EventStore.open(repo / ".aix" / "aix.db")
    try:
        with anyio.fail_after(120):
            outcome = await execute_run(
                RunRequest(project_root=repo, goal="Add a retry option"),
                registry=registry,
                store=store,
                config=config,
            )
        assert outcome.status is RunStatus.COMPLETED, [
            (t.type, t.status, t.failure, t.detail) for t in outcome.tasks
        ]
        assert {t.agent_id for t in outcome.tasks} == {"sample-agent"}
        events = await store.events(run_id=outcome.run_id, types=["check.finished"])
        plugin = [e.payload.check for e in events if e.payload.check.kind is K.CUSTOM]  # type: ignore[attr-defined]
        assert plugin and all(c.status == "passed" for c in plugin)
        assert repos.git(repo, "show", f"{outcome.branch}:feature.py").returncode == 0
    finally:
        await store.close()
