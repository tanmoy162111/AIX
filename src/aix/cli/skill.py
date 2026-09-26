"""`aix skill ...` commands (PLAYBOOK §16, §23.1)."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from aix.cli.common import fail

skill_app = typer.Typer(name="skill", help="Inspect skills.", no_args_is_help=True)

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


@skill_app.command("list")
def list_skills(as_json: JsonOpt = False) -> None:
    """List every available skill."""
    from aix.skills.registry import SkillRegistry

    skills = SkillRegistry.builtin().all()
    if as_json:
        typer.echo(json.dumps([s.meta.model_dump(mode="json") for s in skills], indent=2))
        return
    from rich.console import Console
    from rich.table import Table

    table = Table("name", "task types", "description")
    for s in skills:
        table.add_row(
            s.meta.name, ", ".join(t.value for t in s.meta.task_types), s.meta.description
        )
    Console(width=160).print(table)


@skill_app.command("inspect")
def inspect_skill(
    name: Annotated[str, typer.Argument(help="Skill name.")], as_json: JsonOpt = False
) -> None:
    """Show a skill's metadata, instructions, workflow and verification."""
    from aix.domain.errors import ConfigError
    from aix.skills.registry import SkillRegistry

    try:
        skill = SkillRegistry.builtin().get(name)
    except ConfigError as exc:
        fail(str(exc))
    doc = skill.model_dump(mode="json")
    if as_json:
        typer.echo(json.dumps(doc, indent=2))
        return
    import yaml

    typer.echo(yaml.safe_dump(doc, sort_keys=False, width=100))
