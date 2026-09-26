"""Typer entry point for the `aix` console script.

Kept deliberately thin and import-light (PLAYBOOK §23.4): heavy modules are imported lazily inside
command bodies so that `aix --help` stays fast.
"""

from __future__ import annotations

import typer

from aix import __version__
from aix.cli.agent import agent_app, list_agents
from aix.cli.approvals import approvals, approve, deny
from aix.cli.artifact import artifact_app
from aix.cli.cancel import cancel
from aix.cli.config import config_app
from aix.cli.dev import dev_app
from aix.cli.doctor import doctor
from aix.cli.init import init
from aix.cli.plan import plan_app
from aix.cli.run import run
from aix.cli.skill import skill_app
from aix.cli.stats import stats_app
from aix.cli.status import status
from aix.cli.trace import logs, trace
from aix.cli.verify import review, verify

app = typer.Typer(
    name="aix",
    help="aix — Universal AI Agent Control Plane.",
    no_args_is_help=True,
    add_completion=False,
)

app.command("init")(init)
app.command("doctor")(doctor)
app.command("run")(run)
app.command("cancel")(cancel)
app.command("approvals")(approvals)
app.command("approve")(approve)
app.command("deny")(deny)
app.command("status")(status)
app.command("verify")(verify)
app.command("review")(review)
app.command("trace")(trace)
app.command("logs")(logs)
app.add_typer(agent_app)
app.add_typer(artifact_app)
app.command("agents", help="Alias for `aix agent list`.")(list_agents)
app.add_typer(config_app)
app.add_typer(dev_app)
app.add_typer(plan_app)
app.add_typer(skill_app)
app.add_typer(stats_app)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"aix {__version__}")
        raise typer.Exit()


def _register_known_secrets() -> None:
    """Make credentials from the environment (and the approval token) unwritable (§20.5)."""
    import os

    from aix.security.approvals import token_path
    from aix.security.redact import configure_known_secrets, known_secret_values

    values = known_secret_values(os.environ)
    try:
        token = token_path().read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    configure_known_secrets([*values, token] if token else values)


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the aix version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """Coordinate AI coding agents to produce verified, traceable work."""
    _register_known_secrets()


if __name__ == "__main__":  # pragma: no cover
    app()
