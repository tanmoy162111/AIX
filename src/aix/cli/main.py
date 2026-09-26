"""Typer entry point for the `aix` console script.

Kept deliberately thin and import-light (PLAYBOOK §23.4): heavy modules are imported lazily inside
command bodies so that `aix --help` stays fast.
"""

from __future__ import annotations

import typer

from aix import __version__
from aix.cli.agent import agent_app, list_agents
from aix.cli.cancel import cancel
from aix.cli.config import config_app
from aix.cli.dev import dev_app
from aix.cli.doctor import doctor
from aix.cli.init import init
from aix.cli.plan import plan_app
from aix.cli.run import run
from aix.cli.skill import skill_app

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
app.add_typer(agent_app)
app.command("agents", help="Alias for `aix agent list`.")(list_agents)
app.add_typer(config_app)
app.add_typer(dev_app)
app.add_typer(plan_app)
app.add_typer(skill_app)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"aix {__version__}")
        raise typer.Exit()


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


if __name__ == "__main__":  # pragma: no cover
    app()
