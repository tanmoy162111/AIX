"""`aix dev ...` maintenance commands (PLAYBOOK §23.1)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from pydantic import BaseModel

dev_app = typer.Typer(name="dev", help="Developer and maintenance commands.", no_args_is_help=True)


def all_schema_models() -> dict[str, type[BaseModel]]:
    """Every model whose JSON Schema is committed under ``schemas/``."""
    from aix.domain.schemas import collect_domain_models
    from aix.store.events import EVENT_PAYLOADS

    models: dict[str, type[BaseModel]] = dict(collect_domain_models())
    for event_type, payload in EVENT_PAYLOADS.items():
        models[f"event_{event_type.replace('.', '_')}"] = payload
    return dict(sorted(models.items()))


@dev_app.command("export-schemas")
def export_schemas(
    out: Annotated[Path, typer.Option("--out", help="Directory to write schemas into.")] = Path(
        "schemas"
    ),
) -> None:
    """Write one JSON Schema per model to ``schemas/<name>.json`` and remove stale files."""
    from aix.domain.schemas import render_schema

    models = all_schema_models()
    out.mkdir(parents=True, exist_ok=True)
    for name, model in models.items():
        (out / f"{name}.json").write_text(render_schema(model))
    for stale in out.glob("*.json"):
        if stale.stem not in models:
            stale.unlink()
    typer.echo(f"exported {len(models)} schemas to {out}")


@dev_app.command("rebuild-projections")
def rebuild_projections(
    db: Annotated[Path, typer.Option("--db", help="Path to the project database.")] = Path(
        ".aix/aix.db"
    ),
) -> None:
    """Drop all projection rows and rebuild them by replaying the event log."""
    import anyio

    from aix.store.db import EventStore

    if not db.exists():
        typer.echo(f"no database at {db}; run `aix init` first", err=True)
        raise typer.Exit(2)

    async def _run() -> int:
        store = await EventStore.open(db)
        try:
            return await store.rebuild_projections()
        finally:
            await store.close()

    typer.echo(f"replayed {anyio.run(_run)} events")


@dev_app.command("eval-decisions")
def eval_decisions(
    provider: Annotated[str, typer.Option("--provider", help="rules | jev")] = "rules",
    cases: Annotated[Path, typer.Option("--cases", help="Case file or directory.")] = Path(
        "tests/decision/eval_cases"
    ),
    fail_under: Annotated[
        float | None, typer.Option("--fail-under", help="Exit 1 if accuracy is below this (0..1).")
    ] = None,
    replay: Annotated[
        Path | None, typer.Option("--replay", help="JSON list of DecisionRecords to replay.")
    ] = None,
    policy_hash: Annotated[
        str | None,
        typer.Option("--policy-hash", help="Policy hash the replayed decisions were made under."),
    ] = None,
    save: Annotated[Path | None, typer.Option("--save", help="Write the JSON report here.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the report as JSON.")] = False,
) -> None:
    """Evaluate a decision provider on labeled cases (accuracy, confusion, calibration)."""
    import json
    import os

    import anyio

    from aix.cli.common import EXIT_ENVIRONMENT, fail
    from aix.config.schema import AixConfig
    from aix.decision.eval import load_cases, replay_records, run_eval
    from aix.decision.providers.rules import RulesProvider
    from aix.domain.decisions import DecisionRecord
    from aix.security.policy import Policy

    rules = RulesProvider()
    if provider == "rules":
        engine = rules
    elif provider == "jev":
        from aix.decision.providers.jev import jev_key_env

        if os.environ.get("AIX_LIVE") != "1" or jev_key_env() is None:
            fail(
                "the jev evaluation calls the live service: set AIX_LIVE=1 and "
                "TYPESAFE_API_KEY (or OPENROUTER_API_KEY)",
                EXIT_ENVIRONMENT,
            )
        from aix.decision.providers.jev import TypeSafeJevClient
        from aix.decision.providers.jev_provider import JevDecisionProvider
        from aix.decision.thresholds import Thresholds

        engine = JevDecisionProvider(
            TypeSafeJevClient(timeout_s=20), rules, Thresholds(AixConfig().decision.thresholds)
        )
    else:
        fail(f"unknown provider {provider!r}; use rules or jev")

    if replay is not None:
        raw = json.loads(replay.read_text(encoding="utf-8"))
        records = [DecisionRecord.model_validate(r) for r in raw]
        bad = anyio.run(
            lambda: replay_records(
                engine, records, policy_version=policy_hash or Policy(AixConfig().security).hash
            )
        )
        if as_json:
            typer.echo(json.dumps([b.model_dump() for b in bad], indent=2))
        else:
            typer.echo(f"replayed {len(records)} decisions, {len(bad)} differ")
            for b in bad:
                typer.echo(
                    f"  {b.decision_id}: recorded {b.recorded}, replayed {b.replayed}, "
                    f"hash_ok={b.hash_ok}"
                )
        raise typer.Exit(1 if bad else 0)

    try:
        loaded = load_cases(cases)
    except (OSError, ValueError) as exc:
        fail(f"cannot load cases from {cases}: {exc}")
    report = anyio.run(lambda: run_eval(engine, loaded))
    if save is not None:
        save.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if as_json:
        typer.echo(report.model_dump_json(indent=2))
    else:
        typer.echo(
            f"provider {provider}: {report.correct}/{report.total} correct ({report.accuracy:.1%})"
        )
        for point, row in report.per_point.items():
            typer.echo(f"  {point:<16} n={int(row['n']):<4} accuracy={row['accuracy']:.1%}")
        if report.adversarial_accuracy is not None:
            typer.echo(f"  adversarial      accuracy={report.adversarial_accuracy:.1%}")
        for b in report.calibration:
            typer.echo(f"  confidence [{b.low:.2f},{b.high:.2f}) n={b.n} accuracy={b.accuracy:.1%}")
        for f in report.failures:
            typer.echo(f"  FAIL {f.id}: expected {f.expected}, got {f.actual} {f.reasons}")
    if fail_under is not None and report.accuracy < fail_under:
        raise typer.Exit(1)


@dev_app.command("record")
def record(
    agent: Annotated[str, typer.Argument(help="Adapter id, e.g. claude, codex, gemini, opencode.")],
    out: Annotated[
        Path, typer.Option("--out", help="Recordings root; writes <out>/<agent>/.")
    ] = Path("tests/fixtures/recordings"),
    project: Annotated[Path, typer.Option("--project", help="Project directory.")] = Path("."),
) -> None:
    """Capture a fresh raw stream from a real agent CLI. Needs AIX_LIVE=1 (may cost money)."""
    import os

    import anyio

    from aix.agents.record import record_read_only
    from aix.cli.common import EXIT_ENVIRONMENT, fail, load_or_exit, registry_or_exit
    from aix.domain.errors import AixError

    if os.environ.get("AIX_LIVE") != "1":
        fail("refusing to call a real agent: set AIX_LIVE=1 (live calls may cost money)")
    registry = registry_or_exit(load_or_exit(project))
    if agent not in registry.ids():
        fail(f"unknown agent {agent!r}; known: {', '.join(registry.ids())}")

    async def _run() -> tuple[Path, str, str | None]:
        spec = await registry.probe(agent)
        if spec.health != "ready":
            raise AixError(f"{agent} is not ready: {spec.health_reason}")
        path, outcome = await record_read_only(registry.get(agent), out / agent)
        return path, outcome.status, outcome.stderr_tail

    try:
        path, status, stderr = anyio.run(_run)
    except AixError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(EXIT_ENVIRONMENT) from exc
    typer.echo(f"recorded {path} (status: {status})")
    if status != "completed":
        typer.echo((stderr or "").strip()[-400:], err=True)
        raise typer.Exit(1)
