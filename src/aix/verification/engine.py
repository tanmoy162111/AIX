"""Verification engine: composes all checks for one attempt or tree (PLAYBOOK §17)."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final

import anyio

from aix.config.schema import AixConfig
from aix.domain.enums import CheckKind as K
from aix.domain.ids import IdPrefix, new_id
from aix.domain.tasks import VerificationSpec
from aix.domain.verification import Check, CheckStatus, VerificationReport, compute_overall
from aix.verification.baseline import Baseline, apply_baseline
from aix.verification.checks import run_command_check
from aix.verification.commands import resolve_commands
from aix.verification.gate import CommandGate
from aix.verification.plugins import (
    ActivePluginCheck,
    CheckContext,
    PluginCheckResult,
)
from aix.verification.policy import run_policy_check
from aix.verification.security import run_deps_check, run_sast_check, run_secrets_check

Reviewer = Callable[[str, Sequence[Check]], Awaitable[Check | None]]
"""Reviews ``diff`` given the executed checks; ``None`` means no reviewer was available."""

COMMAND_KINDS: Final = (K.BUILD, K.TESTS, K.LINT, K.TYPECHECK)
CHECK_TIMEOUT_S: Final = 900.0


async def run_verification(
    workspace: Path,
    attempt_id: str,
    spec: VerificationSpec,
    config: AixConfig,
    *,
    patch: str,
    changed_paths: Sequence[str],
    file_scope: Sequence[str],
    baseline: Baseline | None = None,
    out_dir: Path | None = None,
    reviewer: Reviewer | None = None,
    goal: str = "",
    path_env: str | None = None,
    command_gate: CommandGate | None = None,
    plugin_checks: Sequence[ActivePluginCheck] = (),
) -> VerificationReport:
    """Run every applicable check on ``workspace`` and return the report.

    Contract: command checks in ``spec`` run with the resolved (config-overridden) commands and
    are ``required`` per ``spec.required``; a failure that already existed in ``baseline`` is
    downgraded (§17.4). ``policy`` and ``secrets`` (unless ``off``) always run and are required.
    ``sast``/``deps`` run when configured ``on`` or (``auto`` and listed in the spec).
    A ``command_gate`` may refuse a command before it runs (``tool_risk``): that check is
    ``error``. ``ai_review`` runs only if listed and a ``reviewer`` produces a check; it sees the
    executed facts and never alters them. ``plugin_checks`` (configured check plugins) run last as
    ``custom`` checks whose ``required`` comes from configuration; a plugin that is unavailable,
    raises or times out is an ``error`` check. ``overall`` is computed by :func:`compute_overall`,
    so a required check that could not run yields ``incomplete``, never ``passed``.
    """
    listed = set(spec.required) | set(spec.optional)
    required = set(spec.required)
    sec = config.verification.security
    allow = list(config.security.shell_allow)
    network = config.security.network.checks
    commands = resolve_commands(workspace, config.verification.commands)
    checks: list[Check] = []

    for kind in COMMAND_KINDS:
        if kind not in listed:
            continue
        resolved = commands.get(kind)
        if resolved is not None and command_gate is not None:
            refusal = await command_gate(list(resolved.argv))
            if refusal is not None:
                checks.append(
                    Check(
                        id=new_id(IdPrefix.CHECK),
                        kind=kind,
                        status="error",
                        severity="high",
                        required=kind in required,
                        summary=f"command refused: {refusal}",
                        command=list(resolved.argv),
                    )
                )
                continue
        res = await run_command_check(
            kind,
            resolved,
            workspace,
            required=kind in required,
            allow=allow,
            timeout_s=CHECK_TIMEOUT_S,
            network=network,
            out_dir=out_dir / kind.value if out_dir else None,
        )
        checks.append(apply_baseline(res.check, baseline.get(kind) if baseline else None))

    checks.append(await run_policy_check(workspace, list(changed_paths), list(file_scope)))
    secrets = await run_secrets_check(
        patch,
        workspace,
        mode=sec.secrets,
        out_dir=out_dir / "secrets" if out_dir else None,
        path_env=path_env,
    )
    if secrets is not None:
        checks.append(secrets.check)
    if sec.sast == "on" or (sec.sast == "auto" and K.SECURITY_SAST in listed):
        sast = await run_sast_check(
            workspace,
            mode=sec.sast,
            out_dir=out_dir / "sast" if out_dir else None,
            path_env=path_env,
        )
        if sast is not None:
            checks.append(sast.check)
    if sec.deps == "on" or (sec.deps == "auto" and K.DEPS in listed):
        deps = await run_deps_check(
            workspace,
            mode=sec.deps,
            out_dir=out_dir / "deps" if out_dir else None,
            path_env=path_env,
        )
        if deps is not None:
            checks.append(deps.check)
    if K.AI_REVIEW in listed and reviewer is not None:
        review = await reviewer(patch, tuple(checks))
        if review is not None:
            checks.append(review.model_copy(update={"required": K.AI_REVIEW in required}))
    for plugin in plugin_checks:
        checks.append(
            await _run_plugin_check(
                plugin,
                CheckContext(
                    workspace=workspace,
                    patch=patch,
                    changed_paths=tuple(changed_paths),
                    file_scope=tuple(file_scope),
                    goal=goal,
                ),
            )
        )
    return VerificationReport(attempt_id=attempt_id, checks=checks, overall=compute_overall(checks))


_PLUGIN_STATUSES: Final = ("passed", "failed", "warning", "skipped")


async def _run_plugin_check(plugin: ActivePluginCheck, ctx: CheckContext) -> Check:
    """One plugin check as a ``custom`` Check; the plugin cannot crash the engine."""

    def check(status: CheckStatus, summary: str, metrics: dict[str, float], ms: int) -> Check:
        return Check(
            id=new_id(IdPrefix.CHECK),
            kind=K.CUSTOM,
            status=status,
            severity="medium" if status in ("failed", "error") else "info",
            required=plugin.required,
            summary=f"[{plugin.id}] {summary}"[:500],
            metrics=metrics,
            duration_ms=ms,
        )

    if plugin.run is None:
        return check("error", f"plugin unavailable: {plugin.error}", {}, 0)
    started = time.monotonic()
    try:
        with anyio.fail_after(CHECK_TIMEOUT_S):
            result = await plugin.run(ctx)
        ms = int((time.monotonic() - started) * 1000)
        if not isinstance(result, PluginCheckResult) or result.status not in _PLUGIN_STATUSES:
            return check("error", "plugin returned an invalid result", {}, ms)
        return check(result.status, result.summary, dict(result.metrics), ms)
    except TimeoutError:
        return check("error", f"plugin timed out after {CHECK_TIMEOUT_S:.0f}s", {}, 0)
    except Exception as exc:  # third-party code
        return check("error", f"plugin raised {type(exc).__name__}: {exc}", {}, 0)
