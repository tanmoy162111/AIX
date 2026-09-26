"""Skill loading and validation (PLAYBOOK §16)."""

from __future__ import annotations

from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from aix.domain.errors import ConfigError
from aix.skills.schema import Skill, SkillMeta, SkillVerification, Workflow

FILES = ("skill.yaml", "instructions.md", "workflow.yaml", "verification.yaml")


def _read(root: Traversable, name: str, where: str) -> str:
    try:
        return root.joinpath(name).read_text(encoding="utf-8")
    except (OSError, FileNotFoundError) as exc:
        raise ConfigError(f"skill {where}: cannot read {name}: {exc}") from exc


def _parse[M: BaseModel](model: type[M], text: str, name: str, where: str) -> M:
    try:
        doc: Any = yaml.safe_load(text)
        return model.model_validate(doc)
    except yaml.YAMLError as exc:
        raise ConfigError(f"skill {where}: {name} is not valid YAML: {exc}") from exc
    except ValidationError as exc:
        raise ConfigError(f"skill {where}: {name} is invalid: {exc}") from exc


def load_skill(root: Traversable, dirname: str) -> Skill:
    """Load and validate the four skill files under ``root``.

    Contract: returns a :class:`Skill` only if every file exists and passes its schema and the
    skill name equals ``dirname``.

    Raises:
        ConfigError: a file is missing, malformed, invalid, or the name disagrees with the
            directory. The message names the offending file.
    """
    meta = _parse(SkillMeta, _read(root, "skill.yaml", dirname), "skill.yaml", dirname)
    if meta.name != dirname:
        raise ConfigError(f"skill {dirname}: name {meta.name!r} must match its directory")
    instructions = _read(root, "instructions.md", dirname)
    if not instructions.strip():
        raise ConfigError(f"skill {dirname}: instructions.md is empty")
    workflow = _parse(Workflow, _read(root, "workflow.yaml", dirname), "workflow.yaml", dirname)
    verification = _parse(
        SkillVerification, _read(root, "verification.yaml", dirname), "verification.yaml", dirname
    )
    try:
        return Skill(
            meta=meta, instructions=instructions, workflow=workflow, verification=verification
        )
    except ValidationError as exc:
        raise ConfigError(f"skill {dirname}: workflow.yaml is inconsistent: {exc}") from exc


def load_skill_dir(path: Path) -> Skill:
    """Load a skill from a filesystem directory; the directory name is the expected skill name."""
    return load_skill(path, path.name)
